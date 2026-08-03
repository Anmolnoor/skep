"""Stage F (v5): the face — the no-build UI is served by the same daemon."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from skep.supervisor import SupervisorConfig
from skep.supervisor.serve import create_app
from skep.supervisor.serve.app import STATIC_DIR


def test_memory_workspace_routes_and_nav(config: SupervisorConfig) -> None:
    """The web memory workspace: propose -> review -> approve -> search -> forget,
    over the governed routes, plus the nav/view wiring in the static shell."""
    from .conftest import serve_client

    client = serve_client(config)
    note_id = client.post("/api/notes", json={"content": "Deploys via GH Actions"}).json()[
        "note_id"
    ]
    proposal = client.post(
        f"/api/notes/{note_id}/propose", json={"memory_class": "project_fact"}
    ).json()
    pid = proposal["proposal_id"]

    queue = client.get("/api/memory/proposals?state=pending_review").json()["proposals"]
    assert [p["proposal_id"] for p in queue] == [pid]
    assert client.get("/api/memory").json()["items"] == []  # nothing durable yet

    approved = client.post(f"/api/memory/proposals/{pid}/approve").json()
    assert approved["approved"] is True
    memory_id = approved["memory_id"]

    items = client.get("/api/memory").json()["items"]
    assert [i["memory_id"] for i in items] == [memory_id]
    assert len(client.get("/api/memory/search?q=Actions").json()["items"]) == 1

    assert client.delete(f"/api/memory/{memory_id}").json() == {"removed": True}
    assert client.get("/api/memory").json()["items"] == []

    # A second proposal can be rejected with a reason instead.
    p2 = client.post(f"/api/notes/{note_id}/propose", json={"memory_class": "project_fact"}).json()[
        "proposal_id"
    ]
    assert client.post(
        f"/api/memory/proposals/{p2}/reject", json={"reason": "duplicate"}
    ).json() == {"rejected": p2}

    # Nav + view wiring exists in the static shell.
    index = (STATIC_DIR / "index.html").read_text()
    app_js = (STATIC_DIR / "app.js").read_text()
    assert 'href="#/memory"' in index
    assert "viewMemory" in app_js and "#\\/memory" in app_js


def test_shell_has_command_rail_top_bar_and_chat_dock() -> None:
    """ui-redesign: the shell is a command rail + top bar (title/search/Assign)
    + a bottom chat dock. index.html keeps the text-labelled data-ws anchors the
    other structure tests pin; app.js decorates them with icons at boot."""
    index = (STATIC_DIR / "index.html").read_text()
    app_js = (STATIC_DIR / "app.js").read_text()
    css = (STATIC_DIR / "style.css").read_text()

    # Rail + Home nav item.
    assert 'class="sidebar"' in index
    assert 'href="#/home" data-ws="home"' in index
    # Top bar chrome.
    assert 'id="topbar-title"' in index
    assert 'id="app-search"' in index
    assert 'class="topbar-assign primary"' in index and 'href="#/assign"' in index
    # Chat dock.
    assert 'id="dock-form"' in index and 'id="dock-input"' in index
    assert "dock-send" in index

    # Runtime wiring for the icons, search, and dock-launcher.
    assert "function decorateShell" in app_js
    assert "const RAIL_ICONS" in app_js
    assert "function installSearch" in app_js
    assert "function installDock" in app_js
    assert "let pendingChatDraft" in app_js
    # header() feeds the persistent top-bar title instead of an in-content <h2>.
    assert 'document.getElementById("topbar-title")' in app_js

    assert ".sidebar a[data-ws]" in css
    assert ".topbar" in css
    assert ".dock" in css


def test_home_dashboard_view_route_and_stats() -> None:
    """ui-redesign: #/home is the default landing — four stat tiles built from
    existing routes, plus Activity + Waiting on you. v76-F1 re-pin (C4):
    "Total runs" (the least actionable tile) deliberately became "Active
    schedules"; the run totals live in the Runs page's filter counts; the
    verify tile carries the 20-run sparkline; "Recent runs" became the
    activity feed."""
    app_js = (STATIC_DIR / "app.js").read_text()
    css = (STATIC_DIR / "style.css").read_text()

    assert "async function viewHome" in app_js
    assert "[/^#\\/home$/, viewHome]" in app_js
    assert 'const hash = location.hash || "#/home";' in app_js
    assert 'location.hash = "#/home";' in app_js

    assert "The hive at a glance" in app_js
    for label in ["Running now", "Pending approval", "Verify pass rate", "Active schedules"]:
        assert label in app_js
    assert '"Total runs"' not in app_js  # moved consciously, not silently
    assert '"Activity"' in app_js and '"Waiting on you"' in app_js
    # Stats come from routes that already exist (no invented endpoints).
    assert 'api("GET", "/api/runs?limit=500")' in app_js
    assert 'api("GET", "/api/approvals")' in app_js
    assert 'api("GET", "/api/schedules")' in app_js
    assert 'class: "stat-grid"' in app_js and 'class: "home-grid"' in app_js
    # The v75-F1 sparkline helper finds its consumer.
    home = app_js[app_js.index("async function viewHome") : app_js.index("// ---------- Setup")]
    assert "buildSparkline(" in home

    assert ".stat-grid" in css and ".home-grid" in css and ".waiting-card" in css


def test_home_activity_feed_strip_and_banner() -> None:
    """v76-F1: the feed merges runs (updated_at) with approvals
    (requested_at — C5), links only real run pages (#/activity stays a
    dead-route nowhere), the strip reads only enabled/next_run_at/name, and
    the welcome-back banner counts ONLY since-last-visit terminal runs
    behind the 4h threshold (I8)."""
    app_js = (STATIC_DIR / "app.js").read_text()
    css = (STATIC_DIR / "style.css").read_text()

    home = app_js[app_js.index("async function viewHome") : app_js.index("// ---------- Setup")]
    feed = home[home.index("const feedItems") : home.index("const waiting")]
    assert "approval.requested_at" in feed
    assert "created_at" not in feed  # C5: the spec's field does not exist
    assert "#/runs/${run.task_id}" in feed and "#/runs/${approval.task_id}" in feed
    assert "#/activity" not in app_js  # a link to nowhere is a broken promise
    assert '"All runs →"' in home
    assert "Nothing yet — assign a run or ask the Queen." in home
    # The strip claims only real schedule fields, hidden when nothing is due.
    strip = home[home.index("const upcoming") : home.index("const feedItems")]
    assert "s.enabled && s.next_run_at" in strip
    assert "relativeTime(s.next_run_at)" in strip
    # The banner: localStorage stamp, 4h threshold, terminal-since-then only.
    banner = home[home.index("const lastVisit") : home.index("const activeSchedules")]
    assert '"skep-last-visit"' in home
    assert "4 * 60 * 60 * 1000" in banner
    assert "HOME_TERMINAL.has(run.state)" in banner
    assert "new Date(run.updated_at || 0).getTime() > new Date(lastVisit).getTime()" in banner

    for selector in (".activity-list", ".activity-dot-failed", ".schedule-strip", ".welcome-back"):
        assert selector in css, selector
    # C11: the strip scrolls sideways inside the phone breakpoint.
    mobile = css[css.index("/* v75-F1 (C11):") :]
    assert ".schedule-strip { overflow-x: auto;" in mobile


def test_topbar_queen_status_claims_only_sourced_fields() -> None:
    """v76-F2 (C10): the Queen tile shows model + live dot; the hover title
    composes ONLY fields the API returns (num_ctx, num_ctx_source,
    tool_delivery, last_5h.requests). Liveness rides the existing poll — no
    new timer; per-chat context stays in the chat meter."""
    app_js = (STATIC_DIR / "app.js").read_text()
    css = (STATIC_DIR / "style.css").read_text()
    index = (STATIC_DIR / "index.html").read_text()

    assert 'id="topbar-queen-status"' in index
    assert 'id="queen-model-label"' in index

    # The one config fetch fills dock label AND tile; the title's facts are
    # exactly the sourced ones.
    dock = app_js[app_js.index("async function refreshDockModel") :]
    dock = dock[: dock.index("function updateShellChrome")]
    assert ("window ${llm.num_ctx} (${llm.num_ctx_source}) · tools ${llm.tool_delivery}") in dock
    # No invented fields anywhere near the tile (C10/I8).
    status_region = dock + app_js[app_js.index("async function poll()") :]
    assert "last response" not in status_region
    assert "context usage" not in status_region

    # Liveness: poll success → ok, failure → down; usage at most 1/min.
    poll_body = app_js[
        app_js.index("async function poll()") : app_js.index("function schedulePoll")
    ]
    assert 'queenDot.classList.add("ok")' in poll_body
    assert 'queenDot.classList.add("down")' in poll_body
    assert "Date.now() - queenUsageAt > 60000" in poll_body
    assert "req/5h" in poll_body

    for selector in (".topbar-queen-status", ".queen-dot.ok", ".queen-dot.down"):
        assert selector in css, selector
    # C11: the label hides at phone width; the dot stays.
    mobile = css[css.index("/* v75-F1 (C11):") :]
    assert ".topbar-queen-status .queen-model-label { display: none; }" in mobile


def test_projects_cards_and_detail_route() -> None:
    """v76-F3: projects render as cards with phase badges (only the four
    phases the creation form enumerates) and stop being write-only — the
    detail page composes three EXISTING endpoints client-side and keeps the
    effective-policy JSON reachable (I8). Not-found teaches."""
    app_js = (STATIC_DIR / "app.js").read_text()
    css = (STATIC_DIR / "style.css").read_text()

    assert "[/^#\\/projects\\/([^/]+)$/, viewProjectDetail]" in app_js
    assert "#/projects/${project.project_id}" in app_js

    # Exactly the four real phases — no invented fifth. v101-F8 folded the
    # per-phase CSS classes into the .chip primitive, so the vocabulary now
    # lives in PHASE_TONE, and the pin is against projects.PROJECT_PHASES
    # rather than a hand-copied set: the lockstep is with the store, not a
    # literal that drifts.
    import re

    from skep.supervisor.projects import PROJECT_PHASES

    tone_map = app_js[app_js.index("const PHASE_TONE") :]
    tone_map = tone_map[: tone_map.index("}")]
    assert set(re.findall(r"^\s{2}(\w+):", tone_map, re.M)) == set(PROJECT_PHASES)

    detail = app_js[app_js.index("async function viewProjectDetail") :]
    detail = detail[: detail.index("async function viewSchedules")]
    for call in (
        'api("GET", "/api/projects")',
        'api("GET", "/api/schedules")',
        'api("GET", "/api/runs?limit=100")',
    ):
        assert call in detail, call
    # Only reads: the detail page mutates nothing.
    for verb in ('api("POST"', 'api("PUT"', 'api("PATCH"', 'api("DELETE"'):
        assert verb not in detail, verb
    assert '"effective policy"' in detail  # the JSON moved here, reachable
    assert "Project not found" in detail
    assert "buildRunCard" in detail  # rung-2 reuse of the v75 card builder

    # v101-F8: the phase badge folded into the shared .chip primitive.
    assert "phaseChip(project.phase)" in detail
    for selector in (".project-card", ".chip.tone-accent"):
        assert selector in css, selector


def test_assign_guided_flow_and_template_prefill() -> None:
    """v76-F4: Assign is a guided 3-step flow — repo + instructions open,
    template optional, advanced collapsed with field-help — the execution
    choice stays OUTSIDE both collapses beside Dispatch (the review's KEEP),
    a preview line echoes the form only (never the policy engine), and
    ?template= prefill closes the v75-F7 link contract (C9); an unknown
    template teaches (I9)."""
    app_js = (STATIC_DIR / "app.js").read_text()
    css = (STATIC_DIR / "style.css").read_text()

    assign = app_js[app_js.index("async function viewAssign") :]
    assign = assign[: assign.index("// ---------- Runs")]
    assert '"1. What should the worker do?"' in assign
    assert '"2. Pick a template (optional)"' in assign
    assert '"3. Advanced settings"' in assign
    # Execution mode mounts AFTER the advanced collapse — visible, not buried.
    assert assign.index('"3. Advanced settings"') < assign.index('el("label", {}, "execution")')
    # Every advanced knob teaches (the v75-F5 field-help class).
    assert 'el("p", { class: "field-help" }, text)' in assign
    # v101-F10: the caste and engine knobs teach from the registry's own
    # summary rather than a hand-written line, so they build their help element
    # directly instead of calling help() — the count is over both forms.
    assert assign.count("help(") + assign.count('class: "field-help"') >= 7
    # The preview echoes inputs only — no API call in its region.
    preview_region = assign[assign.index("const preview") : assign.index("const help")]
    assert "api(" not in preview_region
    assert "choose execution" in preview_region
    # The prefill: URLSearchParams over the hash query; unknown teaches.
    assert 'new URLSearchParams((location.hash.split("?")[1]) || "")' in assign
    assert 'templateSel.dispatchEvent(new Event("change"))' in assign
    assert "no template named ${wanted} — see #/templates for what exists" in assign
    # Dispatch still POSTs the same endpoint.
    assert 'api("POST", "/api/runs", body)' in assign

    for selector in (".assign-step", ".assign-preview"):
        assert selector in css, selector


def test_approvals_sort_filter_and_age_use_requested_at() -> None:
    """v76-F5: the queue sorts high → medium → low by the existing
    approvalPriority (oldest first within a tier), filters ride approvalKind
    (real actions only), and the waiting clock reads requested_at (C5 — the
    build spec's created_at is a field the payload does not carry). The
    empty state teaches (I9). Verdict wiring is byte-untouched."""
    app_js = (STATIC_DIR / "app.js").read_text()
    css = (STATIC_DIR / "style.css").read_text()

    approvals = app_js[app_js.index("async function viewApprovals") :]
    approvals = approvals[: approvals.index("// ---------- Templates")]
    assert "const PRIORITY_ORDER = { high: 0, medium: 1, low: 2 }" in approvals
    assert "approvalPriority(a)" in approvals
    # Stable within a tier: oldest requested first.
    assert '(a.requested_at || "").localeCompare(b.requested_at || "")' in approvals
    # Filters key off approvalKind — real actions, nothing invented.
    assert 'approvalKind(a) === "shell_run"' in approvals
    assert 'approvalKind(a) === "patch_apply"' in approvals
    # The age: requested_at only, 1h urgent threshold, absolute on hover.
    assert "new Date(approval.requested_at).getTime()" in approvals
    assert "waitedMs > 3600000" in approvals
    assert "title: fmtTs(approval.requested_at)" in approvals
    assert "approval.created_at" not in app_js  # C5, pinned app-wide
    # The empty state teaches.
    assert "Queue is clear — the hive is running smoothly." in approvals
    assert '"Review completed runs →"' in approvals

    assert ".approval-age" in css and ".approval-age.urgent" in css


def test_schedules_relative_next_run_and_health_banners() -> None:
    """v76-F6: next-run is a countdown (past = "overdue") with the absolute
    stamp on hover; failing schedules surface as a warn banner at 3
    consecutive failures with an inline disable at 5 that rides the EXISTING
    PATCH toggle (I5); the banner names the server's own auto-disable-at-5
    guard (I8). A clean page renders no banner."""
    app_js = (STATIC_DIR / "app.js").read_text()
    css = (STATIC_DIR / "style.css").read_text()

    sched = app_js[app_js.index("async function viewSchedules") :]
    sched = sched[: sched.index("// ---------- Policies")]
    # Relative next-run, overdue mapping, absolute hover; last-run absolute.
    assert '? "overdue" : relativeTime(s.next_run_at)' in sched
    assert "title: fmtTs(s.next_run_at)" in sched
    assert 'el("td", {}, fmtTs(s.last_run_at))' in sched
    # Thresholds pinned by number: 3 shows, 5 offers the disable.
    assert "h.consecutive_failures >= 3" in sched
    assert "h.consecutive_failures >= 5 && h.enabled" in sched
    # The disable is the existing PATCH toggle — no new mutation path.
    assert 'api("PATCH", `/api/schedules/${h.name}`, { enabled: false })' in sched
    # The server's own guard is named, honestly.
    assert "the ticker auto-disables a schedule after 5 consecutive failures" in sched
    # Banners gate on actual failures; unreachable providers get the bad one.
    assert "provHealth.filter(h => !h.reachable)" in sched

    for selector in (".health-banner.warn", ".health-banner.bad"):
        assert selector in css, selector


def test_notes_render_markdown_and_tasks_group_by_due() -> None:
    """v76-F7: notes render through the existing renderMarkdown (rung-2
    reuse) with #tag pills and a client-side filter; tasks group Overdue →
    Due today → Upcoming → No due date → Done (collapsed), the checkbox
    rides the existing PATCH, and the EXPLICIT run:<id> token links runs —
    deliberately no bare-hex autolinking (I8). The full editor stays
    reachable."""
    app_js = (STATIC_DIR / "app.js").read_text()
    css = (STATIC_DIR / "style.css").read_text()

    nt = app_js[app_js.index("// ---------- Notes & Tasks (v76-F7") :]
    nt = nt[: nt.index("// ---------- Curated memory")]
    # Markdown + tags + filter; the run token is explicit syntax.
    assert "renderMarkdown(note.content)" in nt
    assert "linkifyRunTokensInPlace(body)" in nt
    assert "/run:([0-9a-f]{6,})/gi" in nt
    assert "\\B#([\\w-]+)" in nt
    assert "Filter notes by text or #tag" in nt
    # The five groups in order, Done collapsed.
    order = [
        '{ label: "Overdue"',
        '{ label: "Due today"',
        '{ label: "Upcoming"',
        '{ label: "No due date"',
        '{ label: "Done", test: t => t.status === "done", collapsed: true }',
    ]
    positions = [nt.index(marker) for marker in order]
    assert positions == sorted(positions)
    # The checkbox is the existing PATCH; the editor keeps every field.
    assert 'checkbox.checked ? "done" : "todo"' in nt
    assert nt.count('api("PATCH", `/api/tasks/${task.task_id}`') == 2  # checkbox + editor
    assert "due.value.trim() || null" in nt
    # The syntax is hinted where it works (I9).
    assert "run:<task-id> links the run." in nt

    # v101-F8: .due-pill folded into .chip; the tone map replaced the
    # per-variant rules, so the pin follows it to where it now lives.
    assert '{ overdue: "tone-bad", today: "tone-warn", upcoming: "tone-muted" }' in nt
    for selector in (
        ".note-tag",
        ".chip.tone-bad",
        ".chip.tone-warn",
        ".task-title.done",
        ".task-group",
    ):
        assert selector in css, selector


def test_memory_chips_and_chat_sidebar_search_pinning() -> None:
    """v76-F8: the memory chip map enumerates EXACTLY the store's
    MEMORY_CLASSES (lockstep — a class added server-side must be added here
    consciously; the spec's invented user/environment/convention list is
    absent); count/size and proposal age render from real fields. The chat
    sidebar gains a client-side search (no API call) and localStorage
    pinning with the pinned section rendered first."""
    import re

    from skep.supervisor import MEMORY_CLASSES

    app_js = (STATIC_DIR / "app.js").read_text()
    css = (STATIC_DIR / "style.css").read_text()

    chip_map = app_js[app_js.index("const MEMORY_CLASS_COLORS") :]
    chip_map = chip_map[: chip_map.index("function memoryClassChip")]
    js_classes = set(re.findall(r"^\s{2}(\w+): \"var\(", chip_map, re.M))
    assert js_classes == set(MEMORY_CLASSES)  # the true lockstep pin

    memory = app_js[app_js.index("async function viewMemory") :]
    memory = memory[: memory.index("// ---------- Deep research")]
    assert "memoryClassChip(p.memory_class)" in memory
    assert "memoryClassChip(item.memory_class)" in memory
    assert "proposed ${relativeTime(p.created_at)}" in memory
    assert "items · ${memKb} KB" in memory

    sidebar = app_js[app_js.index("function renderSidebarChats") :]
    sidebar = sidebar[: sidebar.index("function installShellHandlers")]
    assert "api(" not in sidebar  # search + pin are pure client state (I11)
    assert 'class: "chat-sidebar-search"' in sidebar
    # Pinning round-trips localStorage; pinned renders before the groups.
    assert '"skep-pinned-chats"' in app_js
    assert sidebar.index("sidebar-chat-pinned") < sidebar.index("const groups = new Map()")
    assert "pinnedChats.has(chat.chat_id)) continue;" in sidebar  # moved, not duplicated

    for selector in (
        ".chip",
        ".chip.upper",
        ".chat-sidebar-search",
        ".chat-pin.pinned",
        ".sidebar-chat-pinned-label",
    ):
        assert selector in css, selector
    # v101-F8: the tint mechanism survived the fold into .chip — memory chips
    # still set their own --chip-color inline rather than using a named tone.
    assert "color-mix(in srgb, var(--chip-color, var(--muted)) 15%, transparent)" in css
    assert 'class: "chip upper"' in app_js


def test_schedule_view_renders_health_from_real_routes(config: SupervisorConfig) -> None:
    """v14 Step 8: the schedules view fetches the schedule + provider health
    routes (which exist), and app.js still parses (route-consistency + ES-module
    tests below guard the rest)."""
    app_js = (STATIC_DIR / "app.js").read_text()
    assert "/api/schedules/health" in app_js
    assert "/api/providers/health" in app_js
    assert "/api/nodes" in app_js


def test_mobile_responsive_breakpoints_exist() -> None:
    """v16 Step 7: real media-query breakpoints so the shell is usable at 390px —
    the sidebar collapses, tables scroll, and cards stack."""
    css = (STATIC_DIR / "style.css").read_text()
    assert "@media (max-width: 640px)" in css
    assert "flex-direction: column" in css  # the shell stacks vertically
    assert "overflow-x: auto" in css  # tables scroll instead of overflowing
    assert "grid-template-columns: 1fr" in css  # key/value pairs stack
    # The viewport meta tag makes the breakpoints apply on real devices.
    index = (STATIC_DIR / "index.html").read_text()
    assert "viewport" in index and "width=device-width" in index


def test_research_report_renders_in_a_locked_down_sandboxed_iframe() -> None:
    """v17 Step 6: report.html renders in a sandboxed iframe with NO
    allow-same-origin, and the markdown stays readable as plain text."""
    app_js = (STATIC_DIR / "app.js").read_text()
    assert "function renderResearchReport" in app_js
    assert "sandbox:" in app_js  # the iframe is sandboxed
    assert "allow-same-origin" not in app_js  # never granted
    assert "research-markdown" in app_js  # markdown readable as plain text


def test_index_and_assets_are_public(config: SupervisorConfig) -> None:
    client = TestClient(create_app(config))  # deliberately unauthenticated

    index = client.get("/")
    assert index.status_code == 200
    assert "text/html" in index.headers["content-type"]
    assert "skep" in index.text and "/static/app.js" in index.text

    script = client.get("/static/app.js")
    assert script.status_code == 200
    assert "javascript" in script.headers["content-type"]
    # No-build UI: the browser must revalidate, or an upgrade leaves it
    # running the previous version's modules against the new daemon.
    assert script.headers["cache-control"] == "no-cache"
    assert index.headers["cache-control"] == "no-cache"
    assert "cache-control" not in client.get("/api/status").headers

    styles = client.get("/static/style.css")
    assert styles.status_code == 200

    # Assets are public; the API behind them is not.
    assert client.get("/api/status").status_code == 401


def test_ui_calls_only_routes_that_exist(config: SupervisorConfig) -> None:
    """Every /api/ path mentioned in app.js resolves to a real route."""
    import re

    app = create_app(config)
    known = {route.path for route in app.routes}
    source = (STATIC_DIR / "app.js").read_text()
    for raw in set(re.findall(r"\"(/api/[^\"?$`]*)", source)) | set(
        re.findall(r"`(/api/[^`?]*)`?", source)
    ):
        path = re.sub(r"\$\{[^}]+\}", "{x}", raw).rstrip("/")
        pattern = "^" + re.sub(r"\{[^}]+\}", "[^/]+", path) + "$"
        assert any(re.match(pattern, re.sub(r"\{[^}]+\}", "x", k)) for k in known), (
            f"app.js calls {raw} but no such route exists"
        )


def test_app_js_parses_as_an_es_module(tmp_path: Path) -> None:
    """No build step means no compile check — parse it with node when available."""
    if shutil.which("node") is None:
        pytest.skip("node not installed; parsing is exercised by the browser instead")
    target = tmp_path / "app.mjs"  # .mjs makes --check parse it as a module
    target.write_text((STATIC_DIR / "app.js").read_text())
    result = subprocess.run(["node", "--check", str(target)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_settings_shell_renders_before_model_list_fetch() -> None:
    """A slow/failed model list must not leave Settings showing only its header.
    v75-F3 re-pin (C4): the assistant card now lives in its tab renderer —
    still appended before the model-list fetch."""
    source = (STATIC_DIR / "app.js").read_text()
    assistant = source[source.index("async function renderAssistantTab") :]
    append_settings = assistant.index("panel.append(llmCard)")
    fetch_models = assistant.index('api("GET", "/api/llm/models")')
    assert append_settings < fetch_models


def test_settings_splits_into_tabs() -> None:
    """v75-F3: Settings is five tabs (Assistant / Worker / Channels / Webhooks
    / Repos) on the shared tab bar — one tab's DOM rendered at a time (lazy
    render), with a channels status summary that reads only fields the API
    returns (I8) and per-session tab memory."""
    source = (STATIC_DIR / "app.js").read_text()
    css = (STATIC_DIR / "style.css").read_text()

    settings = source[source.index("async function viewSettings") :]
    settings = settings[: settings.index("// ---------- health poll")]
    for tab in (
        '{ key: "assistant", label: "Assistant", render: renderAssistantTab }',
        '{ key: "worker", label: "Worker", render: renderWorkerTab }',
        '{ key: "channels", label: "Channels", render: renderChannelsTab }',
        '{ key: "webhooks", label: "Webhooks", render: renderWebhooksTab }',
        '{ key: "repos", label: "Repos", render: renderReposTab }',
    ):
        assert tab in settings, tab
    # Per-session tab memory: a save's route() lands back on the same tab.
    assert "let activeSettingsTab" in source
    assert "initial: activeSettingsTab" in source

    # The channels summary claims only enabled / live / secret_configured.
    channels = source[source.index("async function renderChannelsTab") :]
    channels = channels[: channels.index("async function renderWebhooksTab")]
    assert 'class: "channel-summary"' in channels
    assert "channel.enabled && channel.live" in channels
    assert "secret_configured" in channels
    for selector in (".channel-summary", ".channel-status-dot.live", ".channel-status-dot.off"):
        assert selector in css, selector


def test_policy_and_assign_ui_expose_run_budget_knobs() -> None:
    source = (STATIC_DIR / "app.js").read_text()

    for field in [
        "default_wall_clock_seconds",
        "default_max_iterations",
        "default_max_actions",
        "default_max_provider_calls",
    ]:
        assert field in source

    for label in [
        "wall clock (s)",
        "max iterations",
        "max actions",
        "max provider calls",
    ]:
        assert label in source


def test_run_detail_ui_formats_policy_decisions_for_approvals_and_events() -> None:
    source = (STATIC_DIR / "app.js").read_text()

    assert "function formatDecision" in source
    assert "function formatCompactDecision" in source
    assert "function formatRunAutonomy" in source
    assert "function formatPolicyBlock" in source
    assert "function formatProjectContext" in source
    assert "function summarizeRunEvent" in source
    assert "`policy: ${formatDecision(approval.decision)}`" in source
    assert "formatPolicyBlock(approval.policy_block)" in source
    assert "parts.push(`project: ${project}`);" in source
    assert 'if (event.type === "reverify.result") {' in source
    assert 'parts.push(payload.confirmed ? "confirmed" : "not confirmed");' in source
    assert "Array.isArray(payload.commands) && payload.commands.length" in source
    # v65-F2: only claim a re-run when one happened; not_applicable is benign.
    assert '`${ran ? "re-ran" : "recorded verify:"} ${payload.commands.join(", ")}`' in source
    assert 'payload.outcome === "not_applicable"' in source
    assert '"nothing to re-verify — run made no file changes"' in source
    assert "Array.isArray(payload.exit_codes) && payload.exit_codes.length" in source
    assert 'parts.push(`exit ${payload.exit_codes.join(", ")}`);' in source
    assert 'if (event.type === "file.changed") {' in source
    assert "parts.push(`${payload.change} ${payload.path}`);" in source
    assert 'kv("project", `${detail.project_context.project_id} ' in source
    assert "${detail.project_context.strategy}/${detail.project_context.phase})`);" in source
    assert 'kv("binding", `${detail.project_context.binding_kind}: ' in source
    assert "${detail.project_context.binding_value}`);" in source
    assert "`project: ${formatProjectContext(approval.project_context)}`" in source
    assert "const summary = summarizeRunEvent(event);" in source
    assert '{ class: "event", title: raw }' in source


def test_runs_and_schedules_ui_show_project_column() -> None:
    """v75-F4 re-pin (C4): the runs table became cards — the project string
    and the autonomy summary now ride each card instead of columns."""
    source = (STATIC_DIR / "app.js").read_text()

    assert "? `${run.project_context.project_id} (${run.project_context.phase})`" in source
    assert "const autonomy = formatRunAutonomy(run);" in source
    assert "run-card-autonomy" in source  # the autonomy renders as a pill
    assert "parts.push(`d:${dispatch}`);" in source
    assert "parts.push(`l:${landing}`);" in source
    assert (
        '["name", "caste", "project", "every", "on", "next run", "last run", '
        '"last outcome", "source", ""]'
    ) in source
    assert "? `${s.project_context.project_id} (${s.project_context.phase})`" in source
    assert 'el("td", {}, fmtTs(s.last_run_at))' in source
    assert 'el("td", {}, s.last_state || "-")' in source


def test_run_detail_ui_shows_dispatch_and_landing_decisions_in_meta_block() -> None:
    source = (STATIC_DIR / "app.js").read_text()

    assert 'kv("dispatch", formatDecision(detail.dispatch_decision) || "-");' in source
    assert 'kv("landing", formatDecision(detail.landing_decision) || "-");' in source


def test_approvals_ui_shows_run_autonomy_summary() -> None:
    source = (STATIC_DIR / "app.js").read_text()

    assert "const autonomy = formatRunAutonomy(approval.run);" in source
    assert "`autonomy: ${autonomy}`" in source


def test_approvals_card_renders_v19_f1_batch_commands() -> None:
    """The approvals card lists each command a batch (v19-F1) approval grants,
    and the primary button becomes "Approve all N"."""
    source = (STATIC_DIR / "app.js").read_text()
    styles = (STATIC_DIR / "style.css").read_text()

    assert "const batchCommands = Array.isArray(approval.commands)" in source
    assert 'class: "command-list mono"' in source
    assert "`Approve all ${batchCommands.length}`" in source
    assert ".command-list" in styles


def test_templates_ui_exposes_learned_suggestion_flow() -> None:
    source = (STATIC_DIR / "app.js").read_text()
    styles = (STATIC_DIR / "style.css").read_text()

    assert "function renderSuggestionGrant" in source
    assert "function previewTemplateSuggestion" in source
    assert 'api("GET", `/api/suggestions?${query}`)' in source
    assert "confirmTemplateSuggestion" in source
    assert "Preview suggestion" in source
    assert "Confirm suggestion" in source
    assert ".suggestion-grants" in styles


def test_event_summary_shows_project_for_supervisor_approval_events() -> None:
    source = (STATIC_DIR / "app.js").read_text()

    assert 'if (event.type === "approval.requested") {' in source
    assert 'if (event.type === "approval.resolved") {' in source
    assert "const project = formatProjectContext(payload.project_context);" in source
    assert "if (project) parts.push(`project: ${project}`);" in source


def test_projects_ui_exposes_pack_setup_flow() -> None:
    source = (STATIC_DIR / "app.js").read_text()
    index = (STATIC_DIR / "index.html").read_text()

    assert "[/^#\\/projects$/, viewProjects]" in source
    assert (
        'header(main, "Projects", "Trusted packs, bindings, and the schedules they seed.")'
        in source
    )
    assert 'api("GET", "/api/projects")' in source
    assert 'api("POST", "/api/projects/setup", body)' in source
    assert "project_id: projectId.value.trim()" in source
    assert "seed_default_schedules: seedSchedules.checked" in source
    assert 'href="#/projects" data-ws="projects">Projects</a>' in index


def test_projects_ui_supports_pack_preview_before_save() -> None:
    source = (STATIC_DIR / "app.js").read_text()

    assert 'api("GET", "/api/projects/packs")' in source
    assert 'api("POST", "/api/projects/preview", body)' in source
    assert 'el("option", { value: pack.name, disabled: pack.status === "draft" }' in source
    assert '"Preview setup"' in source
    assert "renderProjectPreview" in source
    assert "dangerous_grant_warnings" in source
    assert "seeded_templates" in source
    assert "seeded_schedules" in source
    assert "sample_dispatch_decision" in source
    assert "sample_landing_decision" in source


def test_setup_ui_route_and_guard() -> None:
    source = (STATIC_DIR / "app.js").read_text()
    index = (STATIC_DIR / "index.html").read_text()

    assert "[/^#\\/setup$/, viewSetup]" in source
    assert "async function setupStatus" in source
    assert 'api("GET", "/api/setup/status")' in source
    assert "function setupRouteAllowed" in source
    assert 'hash === "#/setup" || hash === "#/settings"' in source
    assert 'location.hash = "#/setup"' in source
    assert "setup.missing" in source
    assert 'href="#/setup" data-ws="setup">Setup</a>' in index


def test_setup_ui_orchestrates_first_run_endpoints() -> None:
    source = (STATIC_DIR / "app.js").read_text()

    assert "async function viewSetup" in source
    setup = source[source.index("async function viewSetup") : source.index("// ---------- Notes")]
    assert 'api("GET", "/api/llm/config")' in setup
    assert 'api("POST", "/api/llm/test", body)' in setup
    assert 'api("GET", "/api/llm/models")' in setup
    assert 'api("PUT", "/api/llm/config"' in setup
    assert 'api("POST", "/api/setup/default-workspace", { apply:' in setup
    assert 'api("POST", "/api/setup/complete")' in setup
    assert "setup.default_workspace" in setup
    assert "Use default workspace" in setup
    assert "trusted_local_dev" in setup
    assert "project policy overrides" not in setup
    assert "trusted roots" not in setup
    assert "network allowlist" not in setup
    assert "shell allowlist" not in setup
    assert '"advanced policy"' not in setup


def test_chat_ui_uses_canonical_renderer_and_tool_timeline() -> None:
    """Chat history, live streams, and tool events should share the richer UI chrome."""
    source = (STATIC_DIR / "app.js").read_text()
    styles = (STATIC_DIR / "style.css").read_text()

    assert "function renderChatMessage" in source
    assert "function createStreamingReply" in source
    assert "function renderToolEvent" in source
    assert 'log.setAttribute("aria-busy", "true")' in source
    assert ".chat-message-footer" in source
    assert "agent-thread-node" in source

    assert ".chat-message-footer" in styles
    assert ".agent-thread" in styles
    assert ".agent-thread-node" in styles
    assert ".streaming-indicator" in styles
    assert "@media (max-width: 720px)" in styles
    assert ".composer { flex-wrap: wrap; }" in styles


def test_chat_ui_renders_streamed_thinking_inside_assistant_bubble() -> None:
    source = (STATIC_DIR / "app.js").read_text()
    styles = (STATIC_DIR / "style.css").read_text()

    assert "function renderThinkingPanel" in source
    assert "appendThinking(text)" in source
    assert "thinking: (d)" in source
    assert 'el("details", { class: "chat-thinking" }' in source
    # empty turns are skipped; thinking-only turns still render
    assert "(message.content && message.content.trim()) || message.thinking" in source
    assert "thinking: message.thinking" in source
    assert ".chat-thinking" in styles
    assert ".chat-thinking-toggle" in styles
    assert ".chat-thinking-body" in styles


def test_chat_utility_controls_are_icon_only() -> None:
    source = (STATIC_DIR / "app.js").read_text()
    styles = (STATIC_DIR / "style.css").read_text()

    assert "function iconButton" in source
    assert 'class: "chat-message-action icon-button"' in source
    assert 'class: "composer-send icon-button"' in source
    assert 'class: "chat-toolbar-action icon-button ghost"' in source
    assert 'class: "chat-toolbar-action icon-button danger"' in source
    assert 'aria-hidden": "true"' in source

    for visible_text_button in [
        '}, "Copy")',
        '}, "Resend"))',
        '}, "Retry"))',
        '}, "Send")',
        '}, "New chat")',
        '}, "Delete chat")',
        'el("span", { class: "chat-thinking-hint" }, "collapse")',
    ]:
        assert visible_text_button not in source

    assert ".icon-button" in styles
    assert ".composer-send.icon-button" in styles


def test_sidebar_chat_rows_are_one_height() -> None:
    """v88-F1: loose rows are direct children of the column-flex .chat-sidebar,
    so without flex:none they shrink below content height once the list
    overflows — while pinned rows (inside .sidebar-chat-pinned) and grouped rows
    (inside <details>) do not, because their parent is the flex child."""
    styles = (STATIC_DIR / "style.css").read_text()

    assert ".sidebar-chat-item { display: flex" in styles
    assert "flex: none; height: 32px; }" in styles


def test_chat_sidebar_collapses_from_the_toolbar() -> None:
    """v88-F2: the toggle is pure client state — a UI preference in
    localStorage, not server state (I11), like the v76-F8 pins."""
    source = (STATIC_DIR / "app.js").read_text()
    styles = (STATIC_DIR / "style.css").read_text()

    assert ".chat-layout.sidebar-hidden .chat-sidebar { display: none; }" in styles
    assert ".chat-sidebar-toggle { margin-right: auto; }" in styles

    assert 'class: "chat-toolbar-action icon-button ghost chat-sidebar-toggle"' in source
    assert '"skep-chat-sidebar"' in source
    # The toggle never talks to the server, and it announces its state.
    toggle = source[source.index("const applySidebarState") :]
    toggle = toggle[: toggle.index("main.append(layout)")]
    assert "api(" not in toggle
    assert '"aria-expanded"' in toggle
    assert '"show chat list"' in toggle and '"hide chat list"' in toggle
    # The toolbar renders it alongside the existing actions, not in the sidebar
    # itself — a control that hides its own container must outlive it.
    assert 'el("div", { class: "chat-toolbar" }, toggleSidebar, newChat, deleteChat)' in source


def test_confirm_cards_render_the_summary_not_the_model_prose() -> None:
    """v90-F2: all three card renderers share one body — headline, purpose,
    risk — and the model-facing description plus raw args move behind a
    disclosure. The fixed 'nothing runs until you decide' sentences are gone:
    the buttons say that."""
    source = (STATIC_DIR / "app.js").read_text()
    styles = (STATIC_DIR / "style.css").read_text()

    assert "const cardBody = (d)" in source
    assert source.count("...cardBody(d)") == 3  # actionCard, commandCard, gateCard
    # The description and args are reference, reachable but not the default view.
    assert 'el("details", { class: "card-details" }' in source
    # The three restatements are deleted.
    assert "nothing runs until you decide" not in source
    assert "nothing runs until you confirm" not in source
    for selector in (".card-headline", ".card-purpose", ".card-risk", ".card-details"):
        assert selector in styles, selector


def test_a_grant_covered_action_leaves_a_receipt() -> None:
    """v90-F3: silence used to be indistinguishable from nothing happening.

    A receipt is a record, not a decision — same headline and risk as the
    approval card, and deliberately no buttons."""
    source = (STATIC_DIR / "app.js").read_text()
    styles = (STATIC_DIR / "style.css").read_text()

    assert "const receiptCard = (d)" in source
    # It rides the EXISTING tool event: `decision` is present only on an
    # auto-allowed mutation, so nothing that already listens for "tool"
    # (the transcript group, maybeMountWorkerActivity) had to change.
    assert "if (d.decision) { group = null; receiptCard(d); }" in source
    assert ".receipt-card" in styles
    # No verdict buttons on a receipt.
    receipt = source[source.index("const receiptCard = (d)") :]
    receipt = receipt[: receipt.index("// v54-F2")]
    assert "Approve" not in receipt and "Deny" not in receipt
    assert 'class: "actions"' not in receipt

    # The terminal face handles it too — an unhandled event there would
    # recreate the same silence on another surface.
    cli = (STATIC_DIR.parent.parent.parent / "cli_chat.py").read_text()
    assert "ran without asking" in cli


def test_chat_composer_matches_cursor_inspired_chrome() -> None:
    source = (STATIC_DIR / "app.js").read_text()
    styles = (STATIC_DIR / "style.css").read_text()

    # v96-F3: the strip reads server truth — the transcript-scrape heuristic
    # (projectChangeSignal) is deleted, with its render sites. Pin moved WITH
    # the change (C4).
    assert "projectChangeSignal" not in source
    assert "hasCodeContext" not in source
    assert "function contextLoadPercent" in source
    assert "boundProject ? addContext : null" in source
    # v75-F2: the always-disabled codeChrome pills are deleted — a disabled
    # placeholder is a broken promise (I9). The pin moved WITH the change (C4).
    assert "Create Branch & Commit" not in source
    assert "composer-project-chrome" not in source
    assert "composer-pill" not in source
    assert "composer-project-chrome" not in styles
    assert "composer-pill" not in styles
    assert 'class: "composer-add-context icon-button ghost"' in source
    assert 'placeholder: "Ask Skep what to do next"' in source
    assert 'class: "composer-model-select"' in source
    assert 'class: "composer-context-meter"' in source
    # v56-F3: the meter renders SERVER truth (chat detail's context field) —
    # the old client-side heuristic (thinking counted, tool specs ignored) is gone.
    # v74-F4 added the floor segment beside the load.
    assert (
        "style: `--context-load: ${contextLoadPercent(detail?.context)}%; --context-floor: 0%`"
        in source
    )
    assert "context.window_tokens" in source
    assert 'input.addEventListener("input", updateContextMeter)' not in source
    assert 'el("span", {}, "context")' not in source
    assert 'class: "composer-status-row"' in source
    assert "composer-mode-chip" not in source
    assert "composer-mode-chip" not in styles

    assert ".composer-shell" in styles
    assert ".composer-context-meter" in styles
    assert ".composer-status-row" in styles

    # v96-F3: the strip — project selector + branch/policy/engine pills, each
    # rendering the server's own views (effective-policy, repo state), with
    # the same CSS-only popover pattern the context meter uses.
    for needle in (
        'class: "strip-project-select"',
        "/effective-policy`",
        "/state`",
        "policy.execution_mode",
        "policy.coding_engine",
        "policy.verify_command",
        "state.checked_out_branch",
        "policy unresolved",  # I8: an unresolved policy never renders as defaults
        'api("PUT", `/api/chats/${activeChatId}/project`',
        # v96-F4: the buttons PROPOSE the carded verbs — never a direct call.
        'proposeCommand("push_branch", { repo: boundProject.repo, name: stripBranch })',
        'proposeCommand("open_pr", { repo: boundProject.repo, branch: stripBranch })',
        "stripBranch !== state?.default_branch",
    ):
        assert needle in source, needle
    for selector in (
        ".strip-pill",
        ".strip-project-select",
        ".strip-btn",
        ".strip-pill:hover .context-popover",
    ):
        assert selector in styles, selector
    # v74-F4: two ring segments — floor (amber), then conversation (green).
    assert "var(--warn) var(--context-floor, 0%)" in styles
    assert "var(--accent) 0 var(--context-load)" in styles
    assert "line-height: 22px" in styles


def test_live_chat_stream_splits_assistant_bubbles_around_tools() -> None:
    """Live tool use should match refresh replay: assistant, tools, assistant."""
    source = (STATIC_DIR / "app.js").read_text()
    run_stream = source[
        source.index("  const runStream = async") : source.index(
            '  log.append(el("p", { class: "note" },'
        )
    ]
    tool_handler = run_stream[run_stream.index("tool: (d)") : run_stream.index("action: (d)")]
    action_handler = run_stream[run_stream.index("action: (d)") : run_stream.index("error: (d)")]

    assert "let reply = null;" in run_stream
    assert "const currentReply" in run_stream
    assert "const finishReply" in run_stream
    assert run_stream.index("reply = currentReply();") < run_stream.index("await streamSse")
    # v40-F1: tool events append into the current activity group (bubble still
    # finalized first); content deltas and cards close the group.
    assert tool_handler.index("finishReply();") < tool_handler.index("currentGroup().add")
    assert "group = null; currentReply().append(d.content)" in run_stream
    assert action_handler.index("group = null;") < action_handler.index("actionCard")
    assert action_handler.index("finishReply();") < action_handler.index("actionCard")


def test_chat_working_line_covers_the_tool_to_answer_gap() -> None:
    """v55-F7: the blank stretch between a tool result and the next model
    output shows an honest activity line ("Ran <tool> — thinking…") instead
    of nothing; content hides it, turn end removes it."""
    source = (STATIC_DIR / "app.js").read_text()
    styles = (STATIC_DIR / "style.css").read_text()
    run_stream = source[
        source.index("  const runStream = async") : source.index(
            '  log.append(el("p", { class: "note" },'
        )
    ]
    assert 'class: "chat-working"' in run_stream
    tool_handler = run_stream[run_stream.index("tool: (d)") : run_stream.index("action: (d)")]
    # the line appears AFTER the activity group renders — it narrates the gap
    assert tool_handler.index("currentGroup().add") < tool_handler.index("showWorking(")
    # real content hides it; the end of the turn removes it entirely
    assert "hideWorking(); group = null; currentReply().append(d.content)" in run_stream
    assert run_stream.index('log.setAttribute("aria-busy", "false")') < run_stream.index(
        "working.remove()"
    )
    assert ".chat-working" in styles


def test_chat_groups_consecutive_tool_calls_into_activity_rows() -> None:
    """v40-F1 (v35): one collapsed row per burst of tool calls, live and
    replay alike; renderToolEvent survives as the per-member renderer."""
    source = (STATIC_DIR / "app.js").read_text()
    styles = (STATIC_DIR / "style.css").read_text()

    assert "function renderActivityGroup" in source
    assert "function activityGroupSummary" in source
    # deterministic aggregate — computed from summaries, never model text
    assert 'const noun = members.length === 1 ? "1 tool" : `${members.length} tools`;' in source
    # members render through the existing per-call renderer + markup
    assert "renderToolEvent(body, tool, result, memberOptions)" in source
    assert "agent-thread-node" in source
    # the replay loop batches consecutive tool records into one group
    assert "let replayGroup = null;" in source
    assert "if (!replayGroup) replayGroup = renderActivityGroup(log" in source
    # /commands stay free-standing — the deck never folds into model activity
    assert "renderToolEvent(log, title, result, { summary: title, scroll: scrollBottom })" in source

    assert ".activity-group" in styles
    assert ".activity-group-header" in styles
    assert ".activity-group.open .activity-group-body { display: block; }" in styles


def test_tool_summaries_cover_the_supervisor_tools() -> None:
    """v40-F2 (v35): supervisor tools summarize deterministically from the
    tool RESULT json — no bare 'tool: dispatch_run' rows, no model text."""
    source = (STATIC_DIR / "app.js").read_text()

    assert "function unwrapToolResult" in source
    assert "function shortTaskId" in source
    # the summary map reads through the {ok, result} mutation wrapper
    assert "const result = unwrapToolResult(raw);" in source
    for branch in (
        'tool === "dispatch_run"',
        'tool === "get_run"',
        'tool === "list_runs"',
        'tool === "effective_policy"',
        'tool === "repo_state"',
        'tool === "land_run"',
        'tool === "approve_review"',
        'tool === "deny_review"',
    ):
        assert branch in source, branch
    assert "dispatched worker ${shortTaskId(result.task_id)}" in source


def test_worker_activity_block_streams_run_telemetry_inline() -> None:
    """v40-F3 (v35): a dispatched worker renders live IN the chat, by
    reference to the existing run endpoints — read-only, capped, and closed
    on route change."""
    source = (STATIC_DIR / "app.js").read_text()
    styles = (STATIC_DIR / "style.css").read_text()

    assert "function renderWorkerActivity" in source
    assert "function workerRow" in source
    assert "function diffstat" in source
    # pure diffstat: body lines only, file headers skipped
    assert 'if (line.startsWith("+++") || line.startsWith("---")) continue;' in source
    # the mount condition — live and replay both route through it
    assert "DISPATCHED_RESULT_STATES.has(result.state)" in source
    assert "maybeMountWorkerActivity(d.result);" in source
    assert "maybeMountWorkerActivity(result);" in source
    # feed: one replay for terminal runs, EventSource until done for live ones
    assert "new EventSource(`/api/runs/${taskId}/events?stream=1`)" in source
    assert 'source.addEventListener("done"' in source
    # lifecycle: route changes close the streams (v92-F1 folds the status
    # stream and loader ticker into the same teardown); live blocks are capped
    assert "closeLiveWorkerSources();" in source
    assert "const MAX_LIVE_WORKER_BLOCKS = 3;" in source
    assert "liveWorkerSources.size >= MAX_LIVE_WORKER_BLOCKS" in source
    # the operator's asks, verbatim: the running line, the failed-command
    # count, line-by-line output, the approval pointer
    assert "worker running \\u2014 phase:" in source
    assert "` (${failures} failed)`" in source
    assert "stderr_tail" in source
    assert '"waiting for your approval"' in source
    # READ-ONLY: the block never posts — no approve/deny surface here
    block = source[source.index("function workerRow") : source.index("// ---------- Home")]
    assert 'api("POST"' not in block
    assert "streamSse(" not in block

    for selector in (
        ".worker-activity",
        ".worker-activity-gate",
        ".worker-command-output",
        ".worker-activity-diff .diff-add",
    ):
        assert selector in styles, selector


def test_chat_renders_markdown_and_full_tool_results() -> None:
    """Chat fixes: assistant bubbles render markdown (XSS-safe DOM builder, no
    innerHTML) and the streaming path re-renders through it; tool expanders
    are fed by the always-present SSE result."""
    from pathlib import Path

    static_dir = Path("src/skep/supervisor/serve/static")
    app_js = (static_dir / "app.js").read_text(encoding="utf-8")
    assert "function renderMarkdown(" in app_js
    assert "function mdInline(" in app_js
    # wired into both the history path and the streaming path
    assert app_js.count("renderMarkdown(") >= 3
    # the renderer itself must never parse model text as HTML (the one
    # innerHTML elsewhere in the file assigns a static SVG constant)
    start = app_js.index("function mdInline(")
    end = app_js.index("function renderChatMessage(")
    assert "innerHTML" not in app_js[start:end]

    css = (static_dir / "style.css").read_text(encoding="utf-8")
    for selector in (".md-table", ".md-code", ".md-list"):
        assert selector in css


def test_flat_reading_styles_prose_and_mobile_output() -> None:
    """v40-F4 (v35): assistant turns read as prose (the bubble was already
    dropped); the new activity/worker output scrolls sideways on mobile."""
    styles = (STATIC_DIR / "style.css").read_text()

    # assistant messages stay unboxed prose; user bubbles keep their chrome
    assert "background: transparent; padding: 0;" in styles
    assert ".chat-message.assistant .chat-message-role" in styles
    # mobile: expanded output scrolls inside the 720px breakpoint
    mobile = styles[styles.index("@media (max-width: 720px)") :]
    assert ".worker-command-output { white-space: pre; overflow-x: auto; }" in mobile
    # diff stats ride the existing ok/bad tokens — no new palette
    assert ".worker-activity-diff .diff-add { color: var(--ok); }" in styles
    assert ".worker-activity-diff .diff-del { color: var(--bad); }" in styles


def test_user_facing_vocabulary_names_gates_and_templates() -> None:
    """v40-F13 (v36-F9): user-facing language is Policy/Scope/Gate/Template;
    internal names stay. One string-presence pin per renamed surface."""
    source = (STATIC_DIR / "app.js").read_text()
    assert "The gate queue: risky work waits here" in source
    assert "Policy and scopes: autonomy and defaults" in source
    assert "skep setup --template" in source

    docs = Path("docs/configuration.md").read_text(encoding="utf-8")
    assert "## The Vocabulary: Policy, Scope, Gate, Template, Audit" in docs

    import inspect

    from skep.supervisor import cli_cmds

    assert "inspect a run's gate" in inspect.getsource(cli_cmds.register_supervisor_commands)


def test_badge_poll_refreshes_what_it_counts() -> None:
    """v56-F6 (ADR 0038): the approvals list / Home panel re-render when the
    pending count changes, and a card-locked chat unlocks within one poll
    cycle when its cards were resolved on another surface."""
    source = (STATIC_DIR / "app.js").read_text()
    assert "let lastPendingApprovals = null;" in source
    assert "let pendingCardChatId = null;" in source
    poll_body = source[
        source.index("async function poll()") : source.index("function schedulePoll")
    ]
    assert "status.pending_approvals !== lastPendingApprovals" in poll_body
    assert 'hash === "#/approvals"' in poll_body
    assert 'action.status === "proposed"' in poll_body
    # the chat view registers its lock with the poll
    assert "pendingCardChatId = locked ? activeChatId : null;" in source


def test_poll_never_reroutes_while_a_chat_stream_is_active() -> None:
    """v60-F1: confirming a card resolves it server-side seconds before the
    model's continuation finishes streaming; in that window the store shows
    zero proposed cards, so the v56-F6 badge poll called route() mid-stream —
    re-rendering the view and orphaning the live stream's DOM (field test
    2026-07-18: flicker, the follow-up card invisible until it auto-denied).
    The poll now defers to the stream, which reconciles the composer itself."""
    source = (STATIC_DIR / "app.js").read_text()
    assert "pendingCardChatId && !chatStreamActive" in source
    assert "chatStreamActive = true" in source
    # Cleared in the stream's finally — a thrown stream must never leave the
    # poll muzzled forever.
    assert "} finally {\n      chatStreamActive = false;" in source
    assert source.index("chatStreamActive = true") < source.index("chatStreamActive = false")


def test_settings_carries_the_num_ctx_dial() -> None:
    """v74-F1: the context-window dial reaches the settings UI — the field
    exists, posts num_ctx to the existing PUT, and the note states the two
    facts that matter (chars ~= tokens * 4 budgets the replay everywhere;
    only ollama receives the value on the wire)."""
    source = (STATIC_DIR / "app.js").read_text()
    assert "context window (tokens) — empty = auto" in source
    assert "num_ctx: raw ? parseInt(raw, 10) : 0" in source  # v74-F2: 0 = back to auto
    assert 'llm.num_ctx_source === "override"' in source  # only an override fills the field
    assert "chars ≈ tokens x 4" in source
    assert "only the ollama protocol also sends num_ctx on the wire" in source


def test_the_context_meter_splits_floor_from_conversation() -> None:
    """v74-F4 (+ the operator's popover ask): the ring renders floor vs
    conversation as two segments, and hovering opens a breakdown popover
    with the % used, a filled/empty bar, and the exact token numbers."""
    source = (STATIC_DIR / "app.js").read_text()
    css = (STATIC_DIR / "style.css").read_text()
    assert "--context-floor" in source and "--context-floor" in css
    assert 'class: "context-bar"' in source
    assert "context-bar-floor" in source and "context-bar-history" in source
    assert "tool_surface_chars" in source and "system_prompt_chars" in source
    assert "num_ctx_source" in source
    assert ".composer-context-meter:hover .context-popover" in css
    assert "% used of " in source  # the popover leads with the percentage


def test_v75_foundation_tokens_helpers_and_rail_groups() -> None:
    """v75-F1: additive tokens, the grouped rail, and the shared view helpers
    (relativeTime / buildTabBar / buildFilterBar / buildSparkline) land once,
    each with its 640px story (C11). Every pre-v75 token survives."""
    source = (STATIC_DIR / "app.js").read_text()
    css = (STATIC_DIR / "style.css").read_text()
    index = (STATIC_DIR / "index.html").read_text()

    # New tokens are additive; the pre-v75 tokens all survive.
    for token_name in (
        "--accent-2:",
        "--shadow-card:",
        "--shadow-raised:",
        "--transition-fast:",
        "--transition-normal:",
        "--radius-xl:",
        "--accent:",
        "--shadow-pop:",
        "--shadow-focus:",
        "--radius-pill:",
    ):
        assert token_name in css, token_name

    # The rail groups wrap the pinned anchors without touching them.
    for group in ("daily", "manage", "configure"):
        assert f'class="rail-group" data-group="{group}"' in index
    assert ".rail-group" in css

    # The shared helpers exist; the tab bar persists panels across switches
    # (hidden, never rebuilt) and runs a tab's render() exactly once.
    for helper in (
        "function relativeTime",
        "function buildTabBar",
        "function buildFilterBar",
        "function buildSparkline",
    ):
        assert helper in source, helper
    tab_bar = source[source.index("function buildTabBar") : source.index("function buildFilterBar")]
    assert 'panel.classList.toggle("hidden", k !== key)' in tab_bar
    assert "rendered.has(key)" in tab_bar  # lazy render, once

    for selector in (
        ".tab-bar",
        ".tab-button.active",
        ".tab-content",
        ".filter-bar",
        ".filter-tab.active",
        ".filter-count",
        ".stat-sparkline",
        ".spark-ok",
        ".spark-bad",
    ):
        assert selector in css, selector

    # C11: the 640px story — the grouped rail flows horizontally and the new
    # bars scroll sideways inside the mobile breakpoints.
    mobile = css[css.index("/* v75-F1 (C11):") :]
    assert ".rail-group { flex-direction: row; }" in mobile
    assert ".tab-bar, .filter-bar { overflow-x: auto;" in mobile


def test_runs_page_renders_filter_tabs_and_grouped_cards() -> None:
    """v75-F4: Runs is filter tabs + cards + grouped sections. Superseded
    dimming (v19-F8), the searchable class (topbar filtering), and the verify
    outcome all survive the redesign; the ungrouped remainder lands under
    Other — grouped, never dropped (I8)."""
    source = (STATIC_DIR / "app.js").read_text()
    css = (STATIC_DIR / "style.css").read_text()

    runs_view = source[source.index("const RUN_FILTERS") : source.index("// ---------- Run detail")]
    for key in (
        'key: "all"',
        'key: "running"',
        'key: "pending"',
        'key: "completed"',
        'key: "failed"',
    ):
        assert key in runs_view, key
    # Cards keep search + dimming + verify.
    assert (
        'class: `run-card searchable${run.state === "superseded" ? " superseded" : ""}`'
        in runs_view
    )
    assert "run-card-verify" in runs_view
    # Grouped "all" view claims each run once; the rest lands under Other.
    assert '{ label: "Other", filter: null }' in runs_view
    assert "claimed.has(r.task_id)" in runs_view
    assert "buildFilterBar(RUN_FILTERS, counts, renderCards)" in runs_view

    for selector in (
        ".run-card",
        ".run-card.superseded { opacity: .55; }",
        ".run-group-count",
        ".run-card-verify.passed",
    ):
        assert selector in css, selector
    # C11: the 640px story — headers wrap, summaries unclamp.
    mobile = css[css.index("/* v75-F1 (C11):") :]
    assert ".run-card-header { flex-wrap: wrap; }" in mobile
    assert ".run-card-summary { white-space: normal; }" in mobile


def test_policies_sections_help_and_shell_editor() -> None:
    """v75-F5: Policies is four collapsible sections (Execution defaults open,
    Security / Advanced / Auto-approval collapsed), every field carries a
    one-line help hint (I9), and the shell-command editor is argv-safe (C8):
    each row round-trips its command through exact JSON — no whitespace
    splitting anywhere — with a raw-JSON escape hatch."""
    source = (STATIC_DIR / "app.js").read_text()
    css = (STATIC_DIR / "style.css").read_text()

    policies = source[
        source.index("// ---------- Policies (v75-F5") : source.index(
            "// ---------- Settings (v75-F3"
        )
    ]
    assert 'section("Execution defaults", true,' in policies
    assert 'section("Security", false,' in policies
    assert 'section("Advanced", false,' in policies
    assert 'section("Auto-approval", false,' in policies
    # Auto-approval copy survives verbatim (the upgrade plan's KEEP).
    assert "auto-approval (D3: verified + re-verified manifest-only fixes)" in policies
    assert "auto-apply safe dependency fixes" in policies
    # Field help exists and rides every section (spot-pin one per section).
    assert 'class: "field-help"' in source or "field-help" in policies
    assert "Empty = deny all network." in policies
    assert "the cost ceiling" in policies

    # C8: argv-safe editor — rows hold exact JSON arrays; the source never
    # whitespace-splits a command; the raw toggle exists.
    editor = policies[: policies.index("async function viewPolicies")]
    assert "JSON.stringify(command)" in editor
    assert 'JSON.parse(input.value || "[]")' in editor
    assert ".split(/\\s+/)" not in policies  # never split argv on whitespace
    assert '"raw JSON"' in editor and '"row editor"' in editor
    assert "allowed_shell_commands: shellEditor.value()" in policies

    for selector in (".policy-section", ".policy-section-header", ".field-help", ".shell-cmd-row"):
        assert selector in css, selector


def test_run_detail_timeline_tabs_and_copy_id() -> None:
    """v75-F6: run detail gains a visual timeline (canonical path + failure
    tail + unmodeled states APPENDED, never dropped — C6/I8), a tabbed bottom
    with Transitions as the fourth tab (the raw truth stays reachable), a
    copy-ID button, and panels that persist so the live SSE log keeps
    streaming while another tab is open."""
    source = (STATIC_DIR / "app.js").read_text()
    css = (STATIC_DIR / "style.css").read_text()

    # The timeline models the canonical path, replaces the tail on failure,
    # and appends whatever it does not model.
    assert (
        'const TIMELINE_STATES = ["created", "dispatched", "running", '
        '"pending_approval", "completed"]'
    ) in source
    timeline = source[source.index("function buildRunTimeline") :]
    timeline = timeline[: timeline.index("async function viewRunDetail")]
    assert "TIMELINE_FAILURES.has(t.state)" in timeline
    assert "if (modeled.has(t.state)) continue;" in timeline  # append the rest

    detail = source[source.index("async function viewRunDetail") :]
    detail = detail[: detail.index("// ---------- Approvals")]
    # Four tabs, Transitions last; panels filled up front, bar appended once.
    for tab in (
        '{ key: "events", label: "Events" }',
        '{ key: "commands", label: "Commands" }',
        '{ key: "policy", label: "Policy" }',
        '{ key: "transitions", label: "Transitions" }',
    ):
        assert tab in detail, tab
    assert 'tabs.panels.get("events").append(log)' in detail
    assert 'tabs.panels.get("transitions")' in detail
    assert "main.append(tabs.bar, tabs.content)" in detail
    # The live stream wires into the persistent panel (SSE survives switches).
    assert "new EventSource(`/api/runs/${taskId}/events?stream=1`)" in detail
    # Copy-ID: the full id + clipboard via the shared helper.
    assert "copyText(taskId)" in detail
    assert '"copy task ID"' in detail

    for selector in (
        ".run-timeline",
        ".timeline-node.reached .timeline-dot",
        ".timeline-connector",
        ".run-id-row",
    ):
        assert selector in css, selector
    # C11: the timeline scrolls sideways at phone width.
    mobile = css[css.index("/* v75-F1 (C11):") :]
    assert ".run-timeline { overflow-x: auto; }" in mobile


def test_templates_and_skills_split_into_tabs() -> None:
    """v75-F7: Templates & Skills is two tabs (authored vs learned); template
    cards carry a Use link to #/assign?template=<name> (the route regex
    tolerates the query — C9; parsing ships v76-F4); the skill stepper claims
    ONLY states skills.py can produce: draft → tested → approved, rejected as
    the failure branch — no invented sandboxed/reviewed/active (I8)."""
    source = (STATIC_DIR / "app.js").read_text()
    css = (STATIC_DIR / "style.css").read_text()

    assert '{ key: "templates", label: "Templates", render: renderTemplatesTab }' in source
    assert '{ key: "skills", label: "Skills", render: renderSkillsTab }' in source
    # The Use link and the query-tolerant assign route.
    assert "#/assign?template=${encodeURIComponent(t.name)}" in source
    assert "[/^#\\/assign(?:\\?.*)?$/, viewAssign]" in source
    # The honest stepper: exactly the store's lifecycle.
    assert 'const SKILL_STEPS = ["draft", "tested", "approved"]' in source
    stepper = source[source.index("function buildSkillStepper") :]
    stepper = stepper[: stepper.index("async function renderSkillsTab")]
    assert '"rejected"' in stepper  # the failure branch renders honestly
    for invented in ('"sandboxed"', '"reviewed"', '"active"'):
        assert invented not in stepper, f"stepper claims invented state {invented}"

    for selector in (
        ".template-card",
        ".template-card-use",
        ".skill-stepper",
        ".skill-step.current .skill-step-dot",
    ):
        assert selector in css, selector
    # C11: card headers wrap and the stepper scrolls at phone width.
    mobile = css[css.index("/* v75-F1 (C11):") :]
    assert ".template-card-header { flex-wrap: wrap; }" in mobile
    assert ".skill-stepper { overflow-x: auto; }" in mobile


def test_command_palette_navigates_and_never_mutates() -> None:
    """v75-F8 (C3): ⌘K opens a real palette — the topbar hint stops being a
    broken promise (I9). Safety shape (I5/I6): every entry resolves to a
    location.hash navigation; the palette region contains NO api() calls and
    no streamSse() — it can never execute a mutation or resolve a card."""
    source = (STATIC_DIR / "app.js").read_text()
    css = (STATIC_DIR / "style.css").read_text()

    palette = source[
        source.index("// ---------- v75-F8: the ⌘K command palette") : source.index(
            "// The dock is a launcher"
        )
    ]
    # One entry per page — the full data-ws surface is reachable.
    for ws in (
        "home",
        "chat",
        "runs",
        "approvals",
        "assign",
        "projects",
        "schedules",
        "templates",
        "notes",
        "memory",
        "policies",
        "setup",
        "settings",
    ):
        assert f'hash: "#/{ws}"' in palette, ws
    # The hex-id query offers the run jump; Esc closes; ⌘K opens.
    assert "#/runs/${q}" in palette
    assert '"Escape"' in palette
    assert 'event.key.toLowerCase() === "k"' in palette
    # THE pin: navigation only — no API verbs, no streams, no card resolution.
    assert "api(" not in palette
    assert "streamSse(" not in palette
    assert "location.hash = hash" in palette
    # The old focus-search ⌘K binding is gone from installSearch (moved here).
    search = source[source.index("function installSearch") : source.index("// ---------- v75-F8")]
    assert "metaKey" not in search

    for selector in (".palette-overlay", ".palette-item.selected", ".palette-hint"):
        assert selector in css, selector
    # C11: near-full-width at phone width.
    mobile = css[css.index("/* v75-F1 (C11):") :]
    assert ".palette { width: 100%; }" in mobile


def test_pending_cards_reconcile_without_a_reload() -> None:
    """v81-F13: a pending card reaches an open chat through exactly one live
    channel (the SSE `action` event) — so every other path reconciles: the
    stream's finally re-draws what the drop lost, and the poll routes when an
    undrawn card exists. Both key off data-action-id on the card DOM."""
    source = (STATIC_DIR / "app.js").read_text()

    # The id rides the DOM on ALL card kinds (assistant, command, v87-F2 gate).
    assert source.count('"data-action-id": d.action_id') == 3
    # One shared reconciler; replay and the stream finally both use it.
    assert "const renderPendingCards" in source
    assert source.count("renderPendingCards();") >= 2
    finally_start = source.index("chatStreamActive = false")
    finally_block = source[finally_start : source.index("updateContextMeter()", finally_start)]
    assert "renderPendingCards()" in finally_block
    # The poll routes when the open chat holds a card the DOM does not.
    poll = source[source.index("const openChatMatch") :]
    assert '.confirm-card[data-action-id="${action.action_id}"]' in poll
    assert "if (undrawn) route();" in poll
    # v60-F1 stands: never reconcile while a stream is appending to the log.
    assert "!chatStreamActive && hash.match" in source


def test_worker_loader_ticks_from_dispatch_and_announces_terminals_once() -> None:
    """v92-F1 (field test 2026-07-26): the run status is a per-run loader row
    — phase plus a browser-side timer counting from dispatch — not a single
    line that only moves on heartbeat boundaries. Terminals flash once
    (completions too, deduped across the v56-F7 replay window) and redraw the
    transcript so the stored call-to-action line shows without a reload."""
    source = (STATIC_DIR / "app.js").read_text()
    styles = (STATIC_DIR / "style.css").read_text()
    assert 'class: "worker-loader"' in source
    # the browser ticks every second; teardown clears ticker AND status stream
    assert "const runRowsTick = setInterval(drawRunRows, 1000);" in source
    assert "clearInterval(runRowsTick);" in source
    # the server stays the clock authority — each status event resyncs it
    assert "startedAt: Date.now() - (d.elapsed_seconds || 0) * 1000," in source
    # a live dispatch seeds its row instantly; replay leaves it to the stream
    assert "if (chatStreamActive) trackRun(String(result.task_id));" in source
    # completions announce too, once — never re-flashed by a stream reconnect
    assert "if (notifiedTerminalRuns.has(d.task_id)) return;" in source
    assert 'flash("ok", `run ${d.task_id.slice(0, 13)}… completed`)' in source
    # the stored terminal line appears via redraw — never mid-stream or over a draft
    assert "if (!chatStreamActive && !input.value.trim()) route();" in source
    assert ".worker-loader:empty { display: none; }" in styles


def test_working_line_shows_turn_status_with_elapsed_seconds() -> None:
    """v87-F7: the chat-working line renders turn_status phases and counts
    the seconds locally — a long provider prefill or await_runs block reads
    as 'Thinking… · 142s', never as a hung page."""
    source = (STATIC_DIR / "app.js").read_text()
    assert "turn_status: (d) => {" in source
    assert 'showWorking(d.tool ? `Running ${d.tool}…` : "Thinking…")' in source
    # The elapsed counter is client-side and cleared with the stream.
    assert "const workingTick = setInterval(" in source
    assert "clearInterval(workingTick)" in source


def test_working_line_names_its_start_time() -> None:
    """v92-F2: every Queen phase stamps when it began and ticks from the
    first second — "Running dispatch_run… · 11:17 · 5s". The start clock
    anchors phases too short for the old 3-second threshold to count."""
    source = (STATIC_DIR / "app.js").read_text()
    assert "`${text} · ${shortTime(workingSince)}`" in source
    assert "`${workingBase} · ${shortTime(workingSince)} · ${secs}s`" in source


def test_status_stream_reopens_after_deck_commands_and_card_verdicts() -> None:
    """v92-F3 (field test 2026-07-26): /approve re-dispatched a run
    server-side with nobody subscribed — the chat read as stuck while the
    worker ran and finished. Every dispatch-capable lane now reopens the
    status stream; idempotent, since watchStatus closes its predecessor and
    a chat with no runs streams nothing."""
    source = (STATIC_DIR / "app.js").read_text()
    # the two lanes v43-F4 missed: card verdicts and the command deck
    assert "watchStatus();  // v92-F3: a confirmed card can dispatch or revive a run" in source
    deck = source[source.index('if (content.startsWith("/")) {') :]
    assert deck.index("await runSlashCommand(content);") < deck.index("watchStatus();")
    assert deck.index("watchStatus();") < deck.index("input.focus();")


def test_settings_worker_tab_shows_the_roster_above_the_provider_card() -> None:
    """v101-F9: Settings → Worker was four provider inputs and nothing else, so
    the only way to learn skep can run a researcher, a curator, a script or
    Claude Code was to read the source. The tab GAINED a section — the provider
    card is still there, and the tab key is unchanged so its pin does not move
    for cosmetics."""
    source = (STATIC_DIR / "app.js").read_text()

    worker = source[source.index("const presenceChip") :]
    worker = worker[: worker.index("// -- channels (v26)")]

    # Every row is read from the API — nothing about a caste is decided here.
    assert 'api("GET", "/api/workers")' in worker
    assert "roster.castes.map(casteRow)" in worker
    assert "roster.engines.map(engineRow)" in worker
    for field in (
        "c.summary",
        "c.lands",
        "c.needs_provider",
        "c.needs_network",
        "e.summary",
        "e.external",
        "e.binary",
        "c.detail",
        "e.detail",
    ):
        assert field in worker, field
    # Presence is shown with the probe's own detail as the title (I8) — the UI
    # never restates it.
    assert "title: detail" in worker

    # The section it gained did not cost it the one it had.
    assert "Save provider" in worker and 'api("PUT", "/api/settings"' in worker
    assert worker.index("Castes") < worker.index("Worker LLM override")

    # I12: the boundary is stated where the engine is chosen, not in a doc.
    assert "do NOT pass skep's capability layer" in worker
    assert "requires the project to pin a verify_command" in worker
    assert "land only through a human approval" in worker

    # F8's chip, not a fifth copy of one.
    assert "chip tone-ok" in worker and "chip tone-warn" in worker


def test_assign_dispatches_the_whole_roster_and_can_name_an_engine() -> None:
    """v101-F10: the caste select had two hardcoded options, so five of seven
    castes were undispatchable from the UI, and the per-dispatch engine choice
    chat has had since v95-F3 had no control here at all."""
    source = (STATIC_DIR / "app.js").read_text()

    assign = source[source.index("async function viewAssign") :]
    assign = assign[: assign.index("// ---------- Templates")]

    # Built from the API, not a literal — a caste added to the registry is
    # dispatchable with no edit in app.js. The pin is on the SOURCE of the
    # options, because pinning a count would just be the old literal again.
    assert 'api("GET", "/api/workers")' in assign
    assert "casteSelect(roster.castes)" in assign
    assert 'el("option", {}, "coding")' not in source  # every copy is gone

    # The engine control is coding-only and posts `engine` on the existing body.
    assert 'engineField.hidden = caste.value !== "coding"' in assign
    assert 'caste.value === "coding" && engine.value ? { engine: engine.value } : {}' in assign
    # Absent engine means "the project's", which is a different request from
    # naming the builtin one — so it is omitted, not sent empty.
    assert 'el("option", { value: "" }, "(project default)")' in assign

    # No client-side pre-validation that could disagree with the resolver (I5):
    # an absent binary is SHOWN, never filtered out of the options.
    assert "e.present ? e.name" in assign
    assert "roster.engines.filter" not in assign

    # The operator reads the registry's own summary at the moment they choose.
    assert "casteSummary(roster.castes, caste.value)" in assign


def test_every_caste_select_reads_the_registry() -> None:
    """Three selects had the same two-option literal. One helper, one source —
    the F1 defect was five diverged copies of the roster, and a UI that keeps
    its own is the sixth."""
    source = (STATIC_DIR / "app.js").read_text()
    assert source.count("casteSelect(roster.castes)") == 3
    assert 'el("option", {}, "audit")' not in source


def test_run_cards_and_detail_say_who_ran() -> None:
    """v101-F11: with nine castes and four engines a Runs list was a list of
    anonymous work — two runs on the same repo, one by the builtin worker and
    one by Claude Code, were visually identical."""
    source = (STATIC_DIR / "app.js").read_text()

    chips = source[source.index("function workerChips") :]
    chips = chips[: chips.index("function buildRunCard")]

    # Rides F4's columns; nothing is computed in the client.
    assert "run.worker_kind" in chips and "run.coding_engine" in chips
    # A chip that says "builtin" on every run is noise.
    assert 'run.coding_engine !== "builtin"' in chips
    # NULL renders nothing — no "unknown", no guess (I8).
    assert "unknown" not in chips.lower()
    assert ".filter(Boolean)" in chips

    # Both surfaces call the same builder, so they cannot disagree.
    card = source[source.index("function buildRunCard") :]
    card = card[: card.index("async function viewRuns")]
    assert "...workerChips(run)" in card

    detail = source[source.index("async function viewRunDetail") :]
    detail = detail[: detail.index("// ---------- Templates")]
    assert "const who = workerChips(run)" in detail
    assert "if (who.length) kv(" in detail  # absent stays absent

    # "Verified against WHAT" — the commands G10 actually re-ran, from the
    # store's own record rather than the pin it was configured from.
    assert 'kv("re-verify ran"' in detail
    assert "rv.commands.join" in detail


def test_worker_chips_render_the_three_cases(tmp_path: Path) -> None:
    """The text pins above say the branches EXIST; this runs them. Three cases,
    and the third is the one that matters: every run dispatched before v101 has
    NULL columns, and a Runs list that throws on them is a Runs list nobody can
    open (I8)."""
    source = (STATIC_DIR / "app.js").read_text()
    chips = source[source.index("function workerChips") : source.index("function buildRunCard")]

    script = tmp_path / "chips.mjs"
    script.write_text(
        # A stub `el` that records what was asked for — the function is pure,
        # so it needs no DOM.
        "const el = (tag, attrs, ...kids) => ({ class: attrs.class, text: kids.join('') });\n"
        + chips
        + "\nconst show = (run) => JSON.stringify("
        "workerChips(run).map(c => `${c.class}:${c.text}`));\n"
        'console.log(show({ worker_kind: "researcher", coding_engine: null }));\n'
        'console.log(show({ worker_kind: "coding", coding_engine: "builtin" }));\n'
        'console.log(show({ worker_kind: "coding", coding_engine: "claude_code" }));\n'
        "console.log(show({ worker_kind: null, coding_engine: null }));\n"
    )
    result = subprocess.run(["node", str(script)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    lines = [json.loads(line) for line in result.stdout.strip().splitlines()]

    assert lines[0] == ["chip tone-info:researcher"]  # caste, no engine
    assert lines[1] == ["chip tone-info:coding"]  # builtin is noise
    assert lines[2] == ["chip tone-info:coding", "chip tone-warn:claude_code"]
    assert lines[3] == []  # pre-v101: absent


def test_a_steer_typed_mid_turn_is_queued_not_discarded() -> None:
    """v103-F1: runStream sets `send.disabled = true` for the length of a turn
    and the TEXTAREA was never disabled, so deliver() opened with
    `if (send.disabled) return` and an operator's whole message went nowhere —
    no error, no queue, no cue. Reported as "I can't steer it"."""
    source = (STATIC_DIR / "app.js").read_text()

    chat = source[source.index("async function viewChat") :]
    chat = chat[: chat.index("// ---------- Assign")]

    # The drop is gone: a disabled send QUEUES.
    assert "if (!content || send.disabled || !assistantReady) return;" not in chat
    assert "queuedMessage = queuedMessage ? `${queuedMessage}\\n${content}` : content;" in chat
    # Feedback, both transient and persistent — a queue nobody can see is the
    # same defect wearing a queue (I8).
    assert 'flash("ok", "queued — sends when this turn finishes")' in chat
    assert "queued-steer" in chat

    # It actually gets sent: at the end of the turn, and when a card unlocks
    # the composer from any surface.
    assert chat.count("flushQueued()") >= 2
    assert "if (!locked) flushQueued();" in chat

    # The TDZ trap: setComposerLocked runs during the first render and calls
    # flushQueued, so the queue state must be declared ABOVE it.
    assert chat.index("let queuedMessage") < chat.index("const setComposerLocked")
    assert chat.index("let queuedMessage") < chat.index("setComposerLocked(anyCardPending())")
    # And the empty-queue guard must come before any use of `deliver`.
    flush = chat[chat.index("const flushQueued") :]
    flush = flush[: flush.index("\n  };")]
    assert flush.index("if (!queuedMessage") < flush.index("deliver()")


def test_diff_is_fetched_only_when_a_patch_artifact_exists() -> None:
    """v106-F9: no-patch completions were a steady GET /diff 404 drumbeat in
    serve.log — both fetch sites now check the run's artifacts first."""
    app_js = (STATIC_DIR / "app.js").read_text()
    guards = app_js.count('(detail.artifacts || []).some((a) => a.kind === "patch")')
    fetches = app_js.count("`/api/runs/${taskId}/diff`")
    assert guards == fetches == 2


def test_diagnose_run_executes_in_the_kept_worktree_and_teaches_when_gone(
    repo: Path, config: SupervisorConfig, tmp_path: Path
) -> None:
    """v107-F2: one bounded command in the kept evidence — output capped,
    refusals teach (I9), the REST face is the operator's half (ADR 0050)."""
    from skep.supervisor import RunStore
    from skep.supervisor.contracts_io import DEFAULT_BUDGET, mint_task
    from skep.supervisor.serve.actions import diagnose_run

    store = RunStore(config.db_path)
    try:
        workspace = tmp_path / "kept-tree"
        workspace.mkdir()
        (workspace / "clue.txt").write_text("the failing thing\n", encoding="utf-8")
        task = mint_task(workspace=workspace, instructions="x", budget=DEFAULT_BUDGET)
        store.create_run(task, repo=repo, ref=None, execution_mode="sandbox")
        store.transition(task.task_id, "failed", "agent exited 1")

        out = diagnose_run(store, config, task.task_id, command="cat clue.txt")
        assert out["exit_code"] == 0
        assert "the failing thing" in out["stdout"]

        # Output is capped, and the cap says so.
        big = diagnose_run(store, config, task.task_id, command="yes x | head -c 20000")
        assert len(big["stdout"]) <= 10_000 + 32
        assert "truncated" in big["stdout"]

        # A swept tree refuses with the alternative, not a bare no (I9).
        import shutil

        shutil.rmtree(workspace)
        try:
            diagnose_run(store, config, task.task_id, command="true")
            raise AssertionError("must refuse a swept worktree")
        except ValueError as exc:
            assert "dispatch a fresh run" in str(exc)
            assert "get_run" in str(exc)
    finally:
        store.close()


def test_diagnose_run_rest_face(repo: Path, config: SupervisorConfig, tmp_path: Path) -> None:
    from skep.supervisor import RunStore
    from skep.supervisor.contracts_io import DEFAULT_BUDGET, mint_task

    from .conftest import serve_client

    store = RunStore(config.db_path)
    try:
        workspace = tmp_path / "kept"
        workspace.mkdir()
        task = mint_task(workspace=workspace, instructions="x", budget=DEFAULT_BUDGET)
        store.create_run(task, repo=repo, ref=None, execution_mode="sandbox")
        store.transition(task.task_id, "failed", "agent exited 1")
    finally:
        store.close()
    client = serve_client(config)
    ok = client.post(f"/api/runs/{task.task_id}/diagnose", json={"command": "echo hi"})
    assert ok.status_code == 200, ok.text
    assert ok.json()["exit_code"] == 0
    assert "hi" in ok.json()["stdout"]
