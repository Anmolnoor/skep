"""Phase 10 R1 (10E): the review-first console's read-only routes — the
ledger verify badge, the searchable decision log, the one review queue, the
forged tools and the staged provider probe. Every one is a read (I5): the
verdicts stay on the routes that already existed."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from skep.supervisor import SupervisorConfig, policy_v3
from skep.supervisor.policy_v3 import log_decision

from .conftest import serve_client
from .fake_ollama import FakeOllama


def _live(config: SupervisorConfig) -> Path:
    return config.audit_dir / "policy" / "decisions.jsonl"


# ---------- E1: the ledger verify badge ----------


def test_ledger_verify_route_reads_valid_then_broken_at_file_and_line(
    config: SupervisorConfig,
) -> None:
    client = serve_client(config)
    empty = client.get("/api/ledger/verify").json()
    assert empty["status"] == "VALID" and empty["chained"] == 0
    assert empty["line"] == "VALID: no chained rows"
    for n in range(3):
        log_decision(config.audit_dir, {"value": f"row {n}", "verdict": "allow"})
    valid = client.get("/api/ledger/verify").json()
    assert valid["status"] == "VALID" and valid["chained"] == 3 and valid["break"] is None
    assert valid["line"].startswith("VALID: 3 chained rows, head seq 3 sha256 ")
    live = _live(config)
    lines = live.read_text().splitlines()
    lines[1] = lines[1].replace("row 1", "row X", 1)
    live.write_text("".join(f"{line}\n" for line in lines))
    broken = client.get("/api/ledger/verify").json()
    assert broken["status"] == "BROKEN"
    assert broken["break"]["file"] == str(live) and broken["break"]["line"] == 2
    assert broken["line"].startswith(f"BROKEN at {live}:2: ")


def test_ledger_verify_route_says_degraded_coverage_when_a_checkpoint_was_never_kept(
    config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(policy_v3, "CHECKPOINT_EVERY", 2)
    monkeypatch.setattr(policy_v3, "_checkpoint", lambda *a, **k: None)  # the write failed
    for n in range(4):
        log_decision(config.audit_dir, {"value": f"row {n}"})
    view = serve_client(config).get("/api/ledger/verify").json()
    assert view["status"] == "VALID" and view["checkpoints_missing"] == 2
    assert "coverage degraded" in view["line"]


# ---------- E1: the decision log, searched server-side ----------


def _seed_decisions(config: SupervisorConfig) -> None:
    rows = [
        {"task_id": "t1", "engine": "codex", "actor": "acp:codex", "verdict": "allow"},
        {"task_id": "t1", "engine": "codex", "actor": "acp:codex", "verdict": "deny"},
        {"task_id": "t2", "engine": "claude_code", "actor": "acp:claude", "verdict": "allow"},
        {"task_id": "t1", "engine": "claude_code", "actor": "cli", "verdict": "require_approval"},
        {"task_id": "t1", "engine": "codex", "actor": "acp:codex", "source": "observed"},
    ]
    for n, row in enumerate(rows):
        log_decision(
            config.audit_dir,
            {"at": f"2026-11-2{n}T10:00:00Z", "scope": "shell", "action": "run", "value": f"c{n}"}
            | row,
        )


def test_decision_search_filters_task_engine_actor_and_verdict_on_the_server(
    config: SupervisorConfig,
) -> None:
    _seed_decisions(config)
    client = serve_client(config)

    def values(query: str) -> list[str]:
        found = client.get(f"/api/policy/decisions/search?{query}").json()
        return [row["value"] for row in found["rows"]]

    assert values("") == ["c4", "c3", "c2", "c1", "c0"]  # newest first
    assert values("task_id=t1&engine=codex") == ["c4", "c1", "c0"]
    assert values("actor=acp:*") == ["c4", "c2", "c1", "c0"]
    assert values("verdict=allow") == ["c2", "c0"]
    assert values("verdict=ask") == ["c3"]  # require_approval is an ask (I8)
    assert values("verdict=note") == ["c4"]  # a note is not a decision
    found = client.get("/api/policy/decisions/search?task_id=t1&verdict=deny").json()
    assert found["total"] == 1 and found["summary"]["deny"] == 1
    refused = client.get("/api/policy/decisions/search?verdict=maybe")
    assert refused.status_code == 400
    assert "allow, ask, deny, note" in refused.json()["detail"]


def test_decision_search_pages_with_a_cursor_that_new_rows_do_not_shift(
    config: SupervisorConfig,
) -> None:
    _seed_decisions(config)
    client = serve_client(config)
    first = client.get("/api/policy/decisions/search?limit=2").json()
    assert [r["value"] for r in first["rows"]] == ["c4", "c3"]
    assert first["total"] == 5 and first["next_cursor"] is not None
    log_decision(config.audit_dir, {"value": "c5", "verdict": "allow"})  # arrives between pages
    second = client.get(f"/api/policy/decisions/search?limit=2&cursor={first['next_cursor']}")
    page = second.json()
    assert [r["value"] for r in page["rows"]] == ["c2", "c1"]
    last = client.get(f"/api/policy/decisions/search?limit=2&cursor={page['next_cursor']}").json()
    assert [r["value"] for r in last["rows"]] == ["c0"] and last["next_cursor"] is None
    assert client.get("/api/policy/decisions/search?limit=0").status_code == 400


def test_decision_search_by_task_and_engine_answers_in_under_five_seconds(
    config: SupervisorConfig,
) -> None:
    """Phase 10 test 10.3: filter by task_id and engine in < 5 s."""
    path = _live(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as handle:  # a big v1 log, written fast (the reader is what is timed)
        for n in range(40_000):
            row = {
                "at": "2026-11-20T10:00:00Z",
                "task_id": f"task-{n % 400}",
                "engine": ("codex", "claude_code")[n % 2],
                "actor": "acp:codex",
                "scope": "shell",
                "action": "run",
                "value": f"cmd {n}",
                "verdict": "allow",
            }
            handle.write(json.dumps(row) + "\n")
    client = serve_client(config)
    started = time.monotonic()
    found = client.get("/api/policy/decisions/search?task_id=task-8&engine=codex").json()
    assert time.monotonic() - started < 5.0
    assert found["total"] == 100
    assert all(r["task_id"] == "task-8" and r["engine"] == "codex" for r in found["rows"])


# ---------- E2: one review queue, for the Inbox and its badge ----------


def _seed_queue(config: SupervisorConfig, tmp_path: Path) -> dict[str, str]:
    """A mirrored apply_patch gate, a loose shell gate, a standalone card, a
    land_run card that mirrors the patch gate, a tainted memory proposal, a
    captured skill draft, a tested learned skill and a rejected one."""
    from skep.supervisor import RunStore, mint_task
    from skep.supervisor.skills import DRAFT, REJECTED, TESTED, SkillCandidate
    from skep.supervisor.templates import WorkflowTemplate

    store = RunStore(config.db_path)
    try:
        chat = store.create_chat(title="gated", model=None)
        tasks = []
        for state in ("completed", "pending_approval"):
            task = mint_task(workspace=tmp_path / state, instructions=f"a {state} run")
            store.create_run(task, repo=tmp_path, ref=None, execution_mode="sandbox")
            store.transition(task.task_id, state)
            tasks.append(task.task_id)
        patch = store.enqueue_approval(tasks[0], action="apply_patch", reason="land it")
        gate = store.enqueue_approval(tasks[1], action="network.fetch", reason="pypi.org")
        store.add_chat_action(
            chat.chat_id, tool="approve_review", args={"review_id": patch}, source="gate"
        )
        store.add_chat_action(chat.chat_id, tool="land_run", args={"task_id": tasks[0]})
        card = store.add_chat_action(chat.chat_id, tool="read_url", args={"url": "https://x.dev"})
        memory = store.create_memory_proposal(
            memory_class="project_fact", content="uses uv", actor="queen", taint=("untrusted",)
        )

        def skill(name: str, status: str, provenance: str, at: str) -> None:
            template = WorkflowTemplate(name=name, instructions="# do it", provenance=provenance)
            store.add_candidate(
                SkillCandidate(
                    name=name,
                    signature=name,
                    status=status,
                    template=template,
                    source_task_ids=("chat:abc",),
                    occurrences=1,
                    created_at=at,
                    taint=("untrusted",) if provenance == "captured" else (),
                )
            )

        skill("captured-one", DRAFT, "captured", "2026-11-20T10:00:00Z")
        skill("learned-one", TESTED, "learned", "2026-11-20T11:00:00Z")
        skill("gone", REJECTED, "learned", "2026-11-20T12:00:00Z")
    finally:
        store.close()
    return {"patch": patch, "gate": gate, "card": card, "memory": memory.proposal_id}


def test_review_queue_lists_each_waiting_thing_once_riskiest_boundary_first(
    config: SupervisorConfig, tmp_path: Path
) -> None:
    client = serve_client(config)
    ids = _seed_queue(config, tmp_path)
    queue = client.get("/api/review-queue").json()
    got = [(item["kind"], item["id"]) for item in queue["items"]]
    # The two chat cards mirroring the patch gate are the gate, once (I8).
    assert got == [
        ("gate", ids["patch"]),  # patch-landing
        ("gate", ids["gate"]),  # network-egress
        ("card", ids["card"]),  # network-egress, newer
        ("skill", "captured-one"),  # policy-change, oldest first
        ("skill", "learned-one"),
        ("memory", ids["memory"]),
    ]
    assert queue["count"] == 6
    patch, gate, card, captured, learned, memory = queue["items"]
    assert patch["boundary"] == "patch-landing"
    assert [m["tool"] for m in patch["mirrored_by"]] == ["approve_review", "land_run"]
    assert "could not re-verify" in patch["warning"] or "never re-verified" in patch["warning"]
    assert gate["boundary"] == "network-egress" and gate["action"] == "network.fetch"
    assert card["boundary"] == "network-egress" and card["source"] == "assistant"
    assert card["headline"].startswith("read_url")
    assert memory["boundary"] == "policy-change" and "outside content" in memory["taint"]
    assert captured["approvable"] is True and captured["taint"].startswith("⚠ tainted")
    assert learned["approvable"] is True and learned["taint"] is None


def test_review_queue_count_only_is_the_same_membership_the_list_has(
    config: SupervisorConfig, tmp_path: Path
) -> None:
    client = serve_client(config)
    _seed_queue(config, tmp_path)
    full = client.get("/api/review-queue").json()
    light = client.get("/api/review-queue?detail=false").json()
    assert light["count"] == full["count"] == len(light["items"])
    assert [(i["kind"], i["id"]) for i in light["items"]] == [
        (i["kind"], i["id"]) for i in full["items"]
    ]
    assert set(light["items"][0]) == {"kind", "id", "boundary", "at"}  # nothing costly


_PATCH = "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1 +1,2 @@\n x\n+y\n"


def _completed(store: object, tmp_path: Path, name: str, *, patch: bool, **g10: object) -> str:
    """A completed run, with a patch on disk if asked and a G10 record if given."""
    import hashlib

    from skep.supervisor import mint_task

    task = mint_task(workspace=tmp_path / name, instructions=f"run {name}")
    store.create_run(task, repo=tmp_path, ref=None, execution_mode="sandbox")  # type: ignore[attr-defined]
    store.transition(task.task_id, "completed")  # type: ignore[attr-defined]
    if patch:
        path = tmp_path / f"{name}.patch"
        path.write_text(_PATCH)
        digest = hashlib.sha256(_PATCH.encode()).hexdigest()
        store.add_artifact(task.task_id, kind="patch", audit_path=path, sha256=digest)  # type: ignore[attr-defined]
    if g10:
        record = {"worker_outcome": "passed", "commands": ["uv run pytest"], "detail": "ran"}
        store.record_reverification(task.task_id, **(record | g10))  # type: ignore[attr-defined]
    return str(task.task_id)


def test_review_queue_holds_unlanded_patches_and_g10_disagreements_skep_review_walks(
    config: SupervisorConfig, tmp_path: Path
) -> None:
    """R2 H1: a completed run holding an unlanded patch and a run whose G10
    re-verify disagreed wait on the operator before any approval row exists —
    the queue lists both (as `skep review` does), the badge counts them, and
    the row opens the run: there is no inline land."""
    from skep.supervisor import RunStore

    store = RunStore(config.db_path)
    try:
        unlanded = _completed(
            store, tmp_path, "unlanded", patch=True, outcome="passed", confirmed=True,
            exit_codes=[0],
        )  # fmt: skip
        disagreed = _completed(
            store, tmp_path, "disagreed", patch=False, outcome="failed", confirmed=False,
            exit_codes=[1],
        )  # fmt: skip
        _completed(store, tmp_path, "quiet", patch=False)  # nothing to land, nothing waits
    finally:
        store.close()
    client = serve_client(config)
    queue = client.get("/api/review-queue").json()
    runs = {item["id"]: item for item in queue["items"] if item["kind"] == "run"}
    assert set(runs) == {unlanded, disagreed} and queue["count"] == 2
    ok, bad = runs[unlanded], runs[disagreed]
    assert ok["boundary"] == bad["boundary"] == "patch-landing"
    assert ok["g10"] == "CONFIRMED" and ok["warning"] is None and ok["tamper"] is None
    assert ok["files"] == [{"path": "app.py", "added": 1, "removed": 0}]
    assert ok["waiting"]  # the same "waiting" line `skep review` prints
    assert bad["g10"] == "NOT CONFIRMED" and bad["files"] == []
    assert "re-verification" in bad["warning"] or "re-verify" in bad["warning"]
    light = client.get("/api/review-queue?detail=false").json()
    assert light["count"] == 2
    assert {i["id"] for i in light["items"]} == {unlanded, disagreed}
    store = RunStore(config.db_path, readonly=True)
    try:
        assert store.pending_approvals() == []  # reading the queue opened no review
    finally:
        store.close()


def test_a_land_run_card_carries_its_g10_warning_and_tamper_and_is_the_runs_one_row(
    config: SupervisorConfig, tmp_path: Path
) -> None:
    """R2 H2: a standalone land_run card is a patch-landing item like a gate —
    G10's warning and V1's tamper are computed for it from its task — and it is
    that run's one row (no second "run" row beside it)."""
    from skep.supervisor import RunStore

    store = RunStore(config.db_path)
    try:
        tampered = _completed(
            store, tmp_path, "tampered", patch=True, outcome="tampered", confirmed=False,
            exit_codes=[0], tamper_flags=("deleted_test:test_x.py",),
        )  # fmt: skip
        fine = _completed(
            store, tmp_path, "fine", patch=True, outcome="passed", confirmed=True, exit_codes=[0]
        )
        chat = store.create_chat(title="landing", model=None)
        bad_card = store.add_chat_action(
            chat.chat_id, tool="land_run", args={"task_id": tampered, "branch": "skep/maintain"}
        )
        good_card = store.add_chat_action(chat.chat_id, tool="land_run", args={"task_id": fine})
    finally:
        store.close()
    queue = serve_client(config).get("/api/review-queue").json()
    assert [(i["kind"], i["id"]) for i in queue["items"]] == [
        ("card", bad_card),
        ("card", good_card),
    ]
    bad, good = queue["items"]
    assert bad["boundary"] == "patch-landing" and bad["task_id"] == tampered
    assert bad["tamper"].startswith("TEST-TAMPER") and bad["warning"].startswith("TEST-TAMPER")
    assert bad["branch"] == "skep/maintain"
    assert good["warning"] is None and good["tamper"] is None and good["branch"] is None


def test_a_batch_gate_carries_every_command_its_approval_grants(
    config: SupervisorConfig, tmp_path: Path
) -> None:
    """R2 H3: approving a batch gate grants its whole stored command list —
    the row carries that list, not just the reason naming the first."""
    from skep.supervisor import RunStore, mint_task

    store = RunStore(config.db_path)
    try:
        task = mint_task(workspace=tmp_path / "w", instructions="batch")
        store.create_run(task, repo=tmp_path, ref=None, execution_mode="sandbox")
        store.transition(task.task_id, "pending_approval")
        review = store.enqueue_approval(
            task.task_id,
            action="shell.run",
            reason="command: npm install",
            commands=[["npm", "install"], ["npm", "run", "build:it", "a b"]],
        )
    finally:
        store.close()
    (item,) = serve_client(config).get("/api/review-queue").json()["items"]
    assert item["id"] == review
    assert item["commands"] == ["npm install", "npm run build:it 'a b'"]


def test_a_card_the_web_cannot_confirm_says_where_to_answer_it(
    config: SupervisorConfig,
) -> None:
    """R2 H5: the row carries card_refusal(WEB, card) — the same words the
    chat page draws its read-only card with."""
    client = serve_client(config)
    chat_id = client.post("/api/chats", json={}).json()["chat_id"]
    model = client.post(
        f"/api/chats/{chat_id}/commands",
        json={"tool": "set_assistant_model", "args": {"model": "m2"}},
    ).json()["action_id"]
    role = client.post(
        f"/api/chats/{chat_id}/commands", json={"tool": "set_role", "args": {"name": "x"}}
    ).json()["action_id"]
    items = {i["id"]: i for i in client.get("/api/review-queue").json()["items"]}
    assert items[model]["web_refusal"].endswith("answer it in skep chat")
    assert items[role]["web_refusal"] is None


# ---------- E3: the forged tools, as their records bind them ----------


def test_forged_tools_route_shows_each_binding_and_rechecks_copy_and_seal(
    config: SupervisorConfig,
) -> None:
    import hashlib

    from skep.supervisor import RunStore
    from skep.supervisor.forge import ForgedPlugin, save_plugin, seal_venv

    skep_home = config.home.parent
    tools = skep_home / "tools"
    tools.mkdir(parents=True, exist_ok=True)
    (tools / "word-count.py").write_text("print('hi')\n")
    approved = hashlib.sha256(b"print('hi')\n").hexdigest()
    lock = "ab" * 32
    venv = tools / ".venvs" / "pdf" / lock
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "tool").write_text("x")
    sealed = seal_venv(venv)
    store = RunStore(config.db_path)
    try:
        repo = str(skep_home / "forge")
        save_plugin(
            store,
            ForgedPlugin(
                plugin_id="word-count",
                name="word-count",
                purpose="counts words",
                state="active",
                rel_path="tools/word-count.py",
                server_id="forge-word-count",
                approved_hosts=("api.example.com",),
                source_sha256=approved,
                repo=repo,
                task_id="t-unlanded",
            ),
        )
        save_plugin(
            store,
            ForgedPlugin(
                plugin_id="pdf",
                name="pdf",
                purpose="reads pdfs",
                state="active",
                rel_path="tools/pdf.py",
                server_id="forge-pdf",
                source_sha256="ef" * 32,
                lock_sha256=lock,
                venv_sha256=sealed,
                repo=repo,
                task_id="t-unlanded",
            ),
        )
    finally:
        store.close()
    client = serve_client(config)
    found = {t["plugin_id"]: t for t in client.get("/api/forge/tools").json()["tools"]}
    word, pdf = found["word-count"], found["pdf"]
    assert word["state"] == "active" and word["approved_hosts"] == ["api.example.com"]
    assert word["source_sha256"] == approved and word["installed"] == "matches"
    assert word["seal"] == "none" and word["locked"] == []
    assert "has not landed" in word["lock_note"]  # the pins come from the landed lock
    assert pdf["installed"] == "missing" and pdf["seal"] == "sealed"
    assert pdf["lock_sha256"] == lock and pdf["venv_sha256"] == sealed
    (venv / "bin" / "tool").write_text("swapped")
    assert client.get("/api/forge/tools").json()["tools"][1]["seal"] == "broken"


def test_forged_tools_report_a_malformed_landed_lock_on_its_row_and_list_the_rest(
    config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R2 H8: one tool's unparseable landed lock is that row's note — the
    shelf still lists every other tool."""
    from skep.supervisor import RunStore, forge
    from skep.supervisor.forge import ForgedPlugin, save_plugin

    pinned = f"requests==2.32.3 --hash=sha256:{'0' * 64}\n"
    bound = {"bad-lock": "cd" * 32, "good": "", "pinned": forge.sha256_text(pinned)}
    store = RunStore(config.db_path)
    try:
        for name in bound:
            save_plugin(
                store,
                ForgedPlugin(
                    plugin_id=name, name=name, purpose="p", state="active",
                    rel_path=f"tools/{name}.py", server_id=f"forge-{name}",
                    source_sha256="ab" * 32, lock_sha256=bound[name],
                    repo=str(config.home.parent / "forge"), task_id="t",
                ),
            )  # fmt: skip
    finally:
        store.close()
    locks = {
        "bad-lock": "--index-url https://evil.example/simple\n",
        "good": None,
        "pinned": pinned,
    }
    monkeypatch.setattr(forge, "read_candidate", lambda record, branch: ("", locks[record.name]))
    response = serve_client(config).get("/api/forge/tools")
    assert response.status_code == 200
    found = {t["plugin_id"]: t for t in response.json()["tools"]}
    assert set(found) == set(bound)
    assert found["bad-lock"]["locked"] == []
    assert found["bad-lock"]["lock_note"].startswith("the landed lock is invalid: lock line")
    assert found["good"]["lock_note"] == "no lock landed beside the tool: stdlib only"
    assert found["pinned"]["lock_note"] == "the landed lock is the approved one"
    assert found["pinned"]["locked"] and found["pinned"]["locked"][0].startswith("requests==2.32.3")


# ---------- E4: the staged provider probe — test first, save separately ----------


@pytest.fixture()
def ollama() -> Iterator[FakeOllama]:
    server = FakeOllama(api_key="sk-fake").start()
    yield server
    server.stop()


def test_staged_probe_saves_nothing_and_reports_the_detected_context_window(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    ollama.show_context_lengths = {"llama3.2": 131072}
    client = serve_client(config)
    before = client.get("/api/llm/config").json()
    good = client.post(
        "/api/llm/probe", json={"base_url": ollama.base_url, "api_key": "sk-fake"}
    ).json()
    assert good["ok"] is True and good["models"] == 2 and good["model"] == "llama3.2"
    assert good["context_window"] == {
        "window": 131072,
        "max_output": None,
        "source": "provider-api",
    }
    assert "sk-fake" not in json.dumps(good)  # the key never comes back
    bad = client.post("/api/llm/probe", json={"base_url": ollama.base_url, "api_key": "x"}).json()
    assert bad["ok"] is False and "401" in bad["detail"] and "context_window" not in bad
    # A probe is not a save (I8): the connection in use is exactly as it was.
    assert client.get("/api/llm/config").json() == before
    unknown = client.post(
        "/api/llm/probe",
        json={"base_url": ollama.base_url, "api_key": "sk-fake", "default_model": "qwen3:8b"},
    ).json()
    assert unknown["model"] == "qwen3:8b"
    assert unknown["context_window"]["source"] == "default"  # nothing reported it


def test_staged_probe_and_test_share_one_verdict(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    client = serve_client(config)
    body = {"base_url": ollama.base_url, "api_key": "sk-fake"}
    tested = client.post("/api/llm/test", json=body).json()
    probed = client.post("/api/llm/probe", json=body).json()
    assert tested == {"ok": True, "models": 2}  # /api/llm/test is unchanged
    assert {k: probed[k] for k in tested} == tested
    assert client.post("/api/llm/probe", json={}).json() == {
        "ok": False,
        "detail": "no base URL configured",
    }


def test_a_g10_disagreed_run_leaves_the_queue_once_landed_or_denied_by_a_human(
    config: SupervisorConfig, tmp_path: Path
) -> None:
    """R3 J6: a disagreement is "action needed" only while its patch is
    unlanded and no human has denied landing it — like an unlanded patch."""
    from skep.supervisor import RunStore
    from skep.supervisor.cli_cmds import _review_cards
    from skep.supervisor.serve.run_status import run_terminal_text

    store = RunStore(config.db_path)
    try:
        g10 = {"outcome": "failed", "confirmed": False, "exit_codes": [1]}
        landed = _completed(store, tmp_path, "landed", patch=True, **g10)
        denied = _completed(store, tmp_path, "denied", patch=True, **g10)
        waiting = _completed(store, tmp_path, "waiting", patch=True, **g10)
        for task_id, approved in ((landed, True), (denied, False)):
            review = store.enqueue_approval(task_id, action="apply_patch", reason="land")
            store.resolve_approval(review, approved=approved, actor="operator")
        assert run_terminal_text(store, landed, completion_opt_in=False) is None
        notice = run_terminal_text(store, denied, completion_opt_in=False)
        assert notice is not None and notice[1] == "info" and "denied" in notice[0]
        assert [c.task_id for c in _review_cards(store)] == [waiting]
    finally:
        store.close()
    queue = serve_client(config).get("/api/review-queue?detail=false").json()
    assert [i["id"] for i in queue["items"]] == [waiting]


def test_an_open_pr_card_carries_each_runs_g10_warning_and_tamper(
    config: SupervisorConfig, tmp_path: Path
) -> None:
    """R3 J1: open_pr lands each run's patch (when unlanded) and publishes it,
    whatever its boundary label — the card carries every affected run's G10
    warning and V1 tamper, and is each run's one row. Branch mode names no run."""
    from skep.supervisor import RunStore

    store = RunStore(config.db_path)
    try:
        tampered = _completed(
            store, tmp_path, "tampered", patch=True, outcome="tampered", confirmed=False,
            exit_codes=[0], tamper_flags=("deleted_test:test_x.py",),
        )  # fmt: skip
        warned = _completed(
            store, tmp_path, "warned", patch=True, outcome="unavailable", confirmed=False,
            detail="no uv", exit_codes=[],
        )  # fmt: skip
        fine = _completed(
            store, tmp_path, "fine", patch=True, outcome="passed", confirmed=True, exit_codes=[0]
        )
        chat = store.create_chat(title="pr", model=None)
        one = store.add_chat_action(chat.chat_id, tool="open_pr", args={"task_id": tampered})
        group = store.add_chat_action(
            chat.chat_id, tool="open_pr", args={"task_ids": [fine, warned], "title": "t"}
        )
        clean = store.add_chat_action(chat.chat_id, tool="open_pr", args={"task_id": fine})
        branch = store.add_chat_action(
            chat.chat_id, tool="open_pr", args={"branch": "feat", "repo": str(tmp_path)}
        )
    finally:
        store.close()
    items = serve_client(config).get("/api/review-queue").json()["items"]
    assert {i["kind"] for i in items} == {"card"}  # no "run" row beside a card that lands it
    cards = {i["id"]: i for i in items}
    assert cards[one]["tamper"].startswith("TEST-TAMPER")
    assert cards[one]["task_ids"] == [tampered]
    assert cards[group]["task_ids"] == [fine, warned]
    assert cards[group]["tamper"] is None
    assert warned[:13] in cards[group]["warning"] and "re-verify" in cards[group]["warning"]
    assert cards[clean]["warning"] is None and cards[clean]["tamper"] is None
    assert cards[branch]["task_ids"] == [] and cards[branch]["warning"] is None


def test_an_abbreviated_land_run_task_id_resolves_before_dedupe_and_evidence(
    config: SupervisorConfig, tmp_path: Path
) -> None:
    """R3 J9: a land_run card naming its run by a short prefix is that run's
    one row (or its gate's mirror), and its G10 evidence is the run's own —
    never a false "never re-verified"."""
    from skep.supervisor import RunStore

    store = RunStore(config.db_path)
    try:
        fine = _completed(
            store, tmp_path, "fine", patch=True, outcome="passed", confirmed=True, exit_codes=[0]
        )
        gated = _completed(
            store, tmp_path, "gated", patch=True, outcome="passed", confirmed=True, exit_codes=[0]
        )
        review = store.enqueue_approval(gated, action="apply_patch", reason="land it")
        chat = store.create_chat(title="short", model=None)
        card = store.add_chat_action(chat.chat_id, tool="land_run", args={"task_id": fine[:-4]})
        store.add_chat_action(chat.chat_id, tool="land_run", args={"task_id": gated[:-4] + "…"})
    finally:
        store.close()
    items = serve_client(config).get("/api/review-queue").json()["items"]
    assert [(i["kind"], i["id"]) for i in items] == [("gate", review), ("card", card)]
    gate, row = items
    assert [m["tool"] for m in gate["mirrored_by"]] == ["land_run"]
    assert row["task_id"] == fine and row["task_ids"] == [fine]
    assert row["warning"] is None and row["tamper"] is None


def test_a_folded_card_lists_every_item_and_its_confirm_binds_to_that_count(
    config: SupervisorConfig,
) -> None:
    """R3 J2: a folded card's row lists every item an approve executes, and a
    confirm that names how many items it showed is refused (nothing runs)
    when the card holds a different set."""
    from skep.supervisor import RunStore

    client = serve_client(config)
    chat_id = client.post("/api/chats", json={}).json()["chat_id"]
    store = RunStore(config.db_path)
    try:
        card = store.add_chat_action(chat_id, tool="read_url", args={"url": "https://x.dev/a"})
        store.fold_into_chat_action(card, args={"url": "https://x.dev/b"}, tool_call_id=None)
    finally:
        store.close()
    (item,) = client.get("/api/review-queue").json()["items"]
    assert len(item["items"]) == 2
    assert "https://x.dev/a" in item["items"][0] and "https://x.dev/b" in item["items"][1]
    refused = client.post(
        f"/api/chats/{chat_id}/actions/{card}/confirm", json={"keep": "once", "items": 1}
    )
    assert refused.status_code == 409
    assert "2 items" in refused.json()["detail"] and "Nothing ran" in refused.json()["detail"]
    store = RunStore(config.db_path, readonly=True)
    try:
        record = store.get_chat_action(card)
        assert record is not None and record.status == "proposed"
    finally:
        store.close()


def test_an_approval_row_carries_its_own_stored_argv_quoted(
    config: SupervisorConfig, tmp_path: Path
) -> None:
    """R3 J8: the review page reads the commands THIS review grants (the
    stored argv), each joined with shlex quoting — not a run event's list."""
    from skep.supervisor import RunStore, mint_task

    store = RunStore(config.db_path)
    try:
        task = mint_task(workspace=tmp_path / "w", instructions="batch")
        store.create_run(task, repo=tmp_path, ref=None, execution_mode="sandbox")
        store.transition(task.task_id, "pending_approval")
        argv = [["npm", "install"], ["npm", "run", "build:it", "a b"]]
        store.enqueue_approval(task.task_id, action="shell.run", reason="batch", commands=argv)
    finally:
        store.close()
    (row,) = serve_client(config).get("/api/approvals").json()["approvals"]
    assert row["command_lines"] == ["npm install", "npm run build:it 'a b'"]


def test_a_fresh_profile_saves_the_model_its_probe_named(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    """R3 J11: Settings' Save sends the probed model with the staged
    connection; on a fresh profile that model becomes the default."""
    client = serve_client(config)
    assert not client.get("/api/llm/config").json().get("default_model")
    probed = client.post(
        "/api/llm/probe", json={"base_url": ollama.base_url, "api_key": "sk-fake"}
    ).json()
    saved = client.put(
        "/api/llm/config",
        json={"base_url": ollama.base_url, "api_key": "sk-fake", "default_model": probed["model"]},
    )
    assert saved.status_code == 200, saved.text
    assert client.get("/api/llm/config").json()["default_model"] == probed["model"] == "llama3.2"
