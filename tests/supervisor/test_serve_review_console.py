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


def test_the_badge_poll_reads_no_patch_and_walks_only_runs_that_can_still_wait(
    config: SupervisorConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R3 J7: ?detail=false is membership only — no patch file is opened or
    hashed, and the walk is a store query over runs that can still need
    action (an unlanded patch, an open gate, an unanswered disagreement), not
    every completed run ever recorded."""
    import builtins
    from pathlib import Path as _Path

    from skep.supervisor import RunStore

    store = RunStore(config.db_path)
    try:
        waiting = _completed(store, tmp_path, "waiting", patch=True, outcome="passed",
                             confirmed=True, exit_codes=[0])  # fmt: skip
        landed = _completed(store, tmp_path, "landed", patch=True, outcome="passed",
                            confirmed=True, exit_codes=[0])  # fmt: skip
        review = store.enqueue_approval(landed, action="apply_patch", reason="land")
        store.resolve_approval(review, approved=True, actor="operator")
        quiet = _completed(store, tmp_path, "quiet", patch=False)
        walk = [r.task_id for r in store.runs_awaiting_review()]
    finally:
        store.close()
    assert waiting in walk and landed not in walk and quiet not in walk
    client = serve_client(config)
    opened: list[str] = []
    real_open, real_read = builtins.open, _Path.read_bytes

    def watch_open(file: object, *a: object, **kw: object) -> object:
        if str(file).endswith(".patch"):
            opened.append(str(file))
        return real_open(file, *a, **kw)  # type: ignore[call-overload]

    def watch_read(self: _Path) -> bytes:
        if self.suffix == ".patch":
            opened.append(str(self))
        return real_read(self)

    monkeypatch.setattr(builtins, "open", watch_open)
    monkeypatch.setattr(_Path, "read_bytes", watch_read)
    monkeypatch.setattr(_Path, "read_text", lambda self, *a, **kw: watch_read(self).decode())
    light = client.get("/api/review-queue?detail=false").json()
    assert [i["id"] for i in light["items"]] == [waiting]
    assert opened == []


def test_an_item_folded_between_the_count_check_and_the_claim_runs_nothing(
    config: SupervisorConfig, ollama: FakeOllama, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R4 K2: the count is checked again on the claimed snapshot — an item
    folded after the first check and before the claim hands the card back
    (409 "Nothing ran") instead of running a set nobody was shown."""
    from skep.supervisor import RunStore

    client = serve_client(config)
    client.put(
        "/api/llm/config",
        json={"base_url": ollama.base_url, "default_model": "qwen3", "api_key": "sk-fake"},
    )
    chat_id = client.post("/api/chats", json={}).json()["chat_id"]
    store = RunStore(config.db_path)
    try:
        card = store.add_chat_action(chat_id, tool="read_url", args={"url": "https://x.dev/a"})
    finally:
        store.close()
    claim = RunStore.claim_chat_action

    def fold_then_claim(self: RunStore, action_id: str) -> bool:
        self.fold_into_chat_action(action_id, args={"url": "https://x.dev/b"}, tool_call_id=None)
        return claim(self, action_id)

    monkeypatch.setattr(RunStore, "claim_chat_action", fold_then_claim)
    refused = client.post(
        f"/api/chats/{chat_id}/actions/{card}/confirm", json={"keep": "once", "items": 1}
    )
    assert refused.status_code == 409
    assert "2 items" in refused.json()["detail"] and "Nothing ran" in refused.json()["detail"]
    store = RunStore(config.db_path, readonly=True)
    try:
        record = store.get_chat_action(card)
        assert record is not None and record.status == "proposed"
        assert len(record.fold_items()) == 2
    finally:
        store.close()


def test_a_latin_1_patch_neither_500s_the_queue_nor_the_diff(
    config: SupervisorConfig, tmp_path: Path
) -> None:
    """R4 K5: a valid patch whose text is not UTF-8 (a Latin-1 source file)
    is counted and shown with replacement characters — the full queue still
    equals the badge, and the diff route serves it instead of a 500."""
    import hashlib

    from skep.supervisor import RunStore

    raw = (
        "diff --git a/n.txt b/n.txt\n--- a/n.txt\n+++ b/n.txt\n@@ -1,2 +1,2 @@\n x\n"
        "-caf\xe8\n+caf\xe9\n"
    )
    data = raw.encode("latin-1")
    store = RunStore(config.db_path)
    try:
        latin = _completed(
            store, tmp_path, "latin", patch=False, outcome="passed", confirmed=True,
            exit_codes=[0],
        )  # fmt: skip
        path = tmp_path / "latin.patch"
        path.write_bytes(data)
        digest = hashlib.sha256(data).hexdigest()
        store.add_artifact(latin, kind="patch", audit_path=path, sha256=digest)
        gate = _completed(
            store, tmp_path, "gated", patch=False, outcome="passed", confirmed=True,
            exit_codes=[0],
        )  # fmt: skip
        store.add_artifact(gate, kind="patch", audit_path=path, sha256=digest)
        store.enqueue_approval(gate, action="apply_patch", reason="land it")
    finally:
        store.close()
    client = serve_client(config)
    full = client.get("/api/review-queue")
    assert full.status_code == 200, full.text
    badge = client.get("/api/review-queue", params={"detail": "false"}).json()
    assert [(i["kind"], i["id"]) for i in full.json()["items"]] == [
        (i["kind"], i["id"]) for i in badge["items"]
    ]
    rows = {i["id"]: i for i in full.json()["items"]}
    assert rows[latin]["files"] == [{"path": "n.txt", "added": 1, "removed": 1}]
    diff = client.get(f"/api/runs/{latin}/diff")
    # R5-5: decoded reversibly — the two sides differ on screen, byte for byte
    assert diff.status_code == 200
    assert "-caf\\xe8\n+caf\\xe9" in diff.text and "\ufffd" not in diff.text


def test_one_unreadable_patch_is_its_own_rows_note_never_the_queues_500(
    config: SupervisorConfig, tmp_path: Path
) -> None:
    """R4 K5: a patch file the server cannot read says so on its own row
    ("patch unreadable: …"); every other item still lists, and the full
    queue still equals the badge."""
    import os

    from skep.supervisor import RunStore

    store = RunStore(config.db_path)
    try:
        locked = _completed(
            store, tmp_path, "locked", patch=True, outcome="passed", confirmed=True,
            exit_codes=[0],
        )  # fmt: skip
        fine = _completed(
            store, tmp_path, "fine", patch=True, outcome="passed", confirmed=True,
            exit_codes=[0],
        )  # fmt: skip
    finally:
        store.close()
    (tmp_path / "locked.patch").chmod(0)
    try:
        if os.access(tmp_path / "locked.patch", os.R_OK):
            pytest.skip("running as a user who reads mode-000 files")
        client = serve_client(config)
        full = client.get("/api/review-queue")
        assert full.status_code == 200, full.text
        badge = client.get("/api/review-queue", params={"detail": "false"}).json()
        assert {i["id"] for i in full.json()["items"]} == {i["id"] for i in badge["items"]}
        rows = {i["id"]: i for i in full.json()["items"]}
        assert set(rows) == {locked, fine}
        assert rows[locked]["files_note"].startswith("patch unreadable: ")
        assert rows[fine]["files"] == [{"path": "app.py", "added": 1, "removed": 0}]
        diff = client.get(f"/api/runs/{locked}/diff")
        # R5-6: the server is fine; this artifact is not — a 409, never a 500
        assert diff.status_code == 409 and "patch unreadable" in diff.json()["detail"]
        from skep.supervisor.cli_cmds import _moved_since_shown, _Shown

        store = RunStore(config.db_path, readonly=True)
        try:
            record = store.get_run(locked)
            assert record is not None
            moved = _moved_since_shown(store, record, _Shown("completed", None, "unreadable"))
            assert moved is not None and moved.startswith("the patch is unreadable: ")
        finally:
            store.close()
    finally:
        (tmp_path / "locked.patch").chmod(0o644)


def _landed_on(store: object, task_id: str, branch: str) -> None:
    review = store.enqueue_approval(task_id, action="apply_patch", reason="land")  # type: ignore[attr-defined]
    store.resolve_approval(review, approved=True, actor="me", landing_branch=branch)  # type: ignore[attr-defined]


def test_land_run_and_open_pr_chat_cards_show_each_runs_warning_and_tamper(
    config: SupervisorConfig, tmp_path: Path
) -> None:
    """R4 K1: the chat thread's confirm card is where a landing or a PR is
    approved, so it lists every run it lands or publishes — single, grouped,
    and branch mode (the runs whose approved landing named that branch) —
    each with its own G10 warning and V1 tamper. The Inbox row maps the
    branch the same way."""
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
        on_feat = _completed(
            store, tmp_path, "onfeat", patch=True, outcome="unavailable", confirmed=False,
            detail="no uv", exit_codes=[],
        )  # fmt: skip
        _landed_on(store, on_feat, "feat")
        _landed_on(store, fine, "other")
        chat = store.create_chat(title="land", model=None)
        land = store.add_chat_action(chat.chat_id, tool="land_run", args={"task_id": tampered})
        group = store.add_chat_action(
            chat.chat_id, tool="open_pr", args={"task_ids": [fine, warned], "title": "t"}
        )
        branch = store.add_chat_action(
            chat.chat_id, tool="open_pr", args={"branch": "feat", "repo": str(tmp_path)}
        )
    finally:
        store.close()
    client = serve_client(config)
    actions = {
        a["action_id"]: a["card"]
        for a in client.get(f"/api/chats/{chat.chat_id}").json()["actions"]
    }
    (single,) = actions[land]["runs"]
    assert single["task_id"] == tampered and single["tamper"].startswith("TEST-TAMPER")
    grouped = {r["task_id"]: r for r in actions[group]["runs"]}
    assert list(grouped) == [fine, warned]
    assert grouped[fine]["warning"] is None and grouped[fine]["tamper"] is None
    assert "re-verify" in grouped[warned]["warning"]
    (by_branch,) = actions[branch]["runs"]
    assert by_branch["task_id"] == on_feat and "re-verify" in by_branch["warning"]
    rows = {i["id"]: i for i in client.get("/api/review-queue").json()["items"]}
    assert rows[branch]["task_ids"] == [on_feat] and "re-verify" in rows[branch]["warning"]


def test_a_resolved_landing_card_reads_its_record_and_asks_nothing_live(
    config: SupervisorConfig, tmp_path: Path
) -> None:
    """R4 K1: only a pending card reads the runs live; a resolved one renders
    from what was recorded (W39), so no runs list is invented after the fact."""
    from skep.supervisor import RunStore

    store = RunStore(config.db_path)
    try:
        warned = _completed(
            store, tmp_path, "warned", patch=True, outcome="unavailable", confirmed=False,
            detail="no uv", exit_codes=[],
        )  # fmt: skip
        chat = store.create_chat(title="land", model=None)
        card = store.add_chat_action(chat.chat_id, tool="land_run", args={"task_id": warned})
        store.resolve_chat_action(card, status="denied", result={"ok": False, "denied": True})
    finally:
        store.close()
    (action,) = serve_client(config).get(f"/api/chats/{chat.chat_id}").json()["actions"]
    assert "runs" not in action["card"]


def _every_card_tool() -> list[str]:
    from skep.supervisor.serve.tools import COMMAND_ONLY_TOOLS, COMMAND_TOOL_NAMES, TOOL_SPECS

    names = {t["function"]["name"] for t in TOOL_SPECS} | COMMAND_TOOL_NAMES | COMMAND_ONLY_TOOLS
    return sorted(names)


@pytest.mark.parametrize("tool", _every_card_tool())
def test_no_chat_card_is_inline_approvable_whatever_its_tool(tool: str) -> None:
    """R4 K1 (SD-2): the one allowlist is server-side — a chat card of any
    tool, including one added later, is never approved from an Inbox row,
    whatever its evidence says."""
    from skep.supervisor.serve.console import inline_approvable

    card = {"kind": "card", "tool": tool, "boundary": "in-sandbox-locked", "items": None}
    assert inline_approvable(card) is False


def test_inline_approve_is_only_a_gate_or_memory_whose_row_holds_all_its_evidence() -> None:
    """R4 K1 (SD-2, SD-2b): a non-landing worker gate (every stored command
    listed, ≤ 10) or a memory proposal (its full text); never a landing gate,
    clean or warned (its evidence is the diff) — nor a run, a skill, an
    unknown kind."""
    from skep.supervisor.serve.console import inline_approvable

    gate = {"kind": "gate", "boundary": "host-shell", "commands": ["npm install", "npm test"]}
    assert inline_approvable(gate) is True
    assert inline_approvable({**gate, "commands": [f"c {n}" for n in range(11)]}) is False
    landing = {"kind": "gate", "boundary": "patch-landing", "commands": []}
    assert inline_approvable(landing) is False
    assert inline_approvable({**landing, "warning": "could not re-verify"}) is False
    assert inline_approvable({**landing, "tamper": "TEST-TAMPER"}) is False
    assert inline_approvable({"kind": "memory", "content": "uses uv"}) is True
    assert inline_approvable({"kind": "memory", "content": ""}) is False
    for kind in ("run", "skill", "something-new"):
        assert inline_approvable({"kind": kind, "approvable": True}) is False


def test_the_queue_sends_inline_approve_on_each_item(
    config: SupervisorConfig, tmp_path: Path
) -> None:
    """R4 K1: the Inbox renders Approve from the item's own boolean."""
    from skep.supervisor import RunStore, mint_task

    store = RunStore(config.db_path)
    try:
        task = mint_task(workspace=tmp_path / "w", instructions="gate")
        store.create_run(task, repo=tmp_path, ref=None, execution_mode="sandbox")
        store.transition(task.task_id, "pending_approval")
        review = store.enqueue_approval(
            task.task_id, action="shell.run", reason="batch", commands=[["npm", "install"]]
        )
        chat = store.create_chat(title="c", model=None)
        card = store.add_chat_action(chat.chat_id, tool="set_role", args={"name": "x"})
    finally:
        store.close()
    rows = {i["id"]: i for i in serve_client(config).get("/api/review-queue").json()["items"]}
    assert rows[review]["inline_approve"] is True
    assert rows[card]["inline_approve"] is False


def test_save_binds_to_the_endpoint_its_test_resolved(
    config: SupervisorConfig, ollama: FakeOllama
) -> None:
    """R4 K6: the probe says which endpoint it resolved; Save sends that back
    and a save that would now resolve elsewhere — a blank URL after the saved
    connection moved — is refused ("test again"), nothing written."""
    other = FakeOllama(api_key="sk-other").start()
    try:
        client = serve_client(config)
        put = {"base_url": ollama.base_url, "default_model": "qwen3", "api_key": "sk-fake"}
        assert client.put("/api/llm/config", json=put).is_success
        probed = client.post("/api/llm/probe", json={"protocol": "ollama"}).json()
        assert probed["ok"] and probed["endpoint"] == {
            "base_url": ollama.base_url.rstrip("/"),
            "protocol": "ollama",
        }
        same = client.put(
            "/api/llm/config",
            json={
                "protocol": "ollama",
                "default_model": probed["model"],
                "tested": probed["endpoint"],
            },
        )
        assert same.status_code == 200, same.text
        moved = {"base_url": other.base_url, "default_model": "qwen3", "api_key": "sk-other"}
        assert client.put("/api/llm/config", json=moved).is_success
        refused = client.put(
            "/api/llm/config",
            json={
                "protocol": "ollama",
                "default_model": probed["model"],
                "tested": probed["endpoint"],
            },
        )
        assert refused.status_code == 409
        assert "test again" in refused.json()["detail"]
        assert client.get("/api/llm/config").json()["base_url"].rstrip(
            "/"
        ) == other.base_url.rstrip("/")
    finally:
        other.stop()


def test_a_branch_mode_card_lists_only_its_own_repos_landings_on_that_branch(
    config: SupervisorConfig, tmp_path: Path
) -> None:
    """R5-1: skep/maintain is the same name in every maintain-phase repo — a
    branch-mode open_pr card for repo A resolves A as open_pr does and lists
    only A's run landed there, never repo B's (nor B's G10 / V1)."""
    from skep.supervisor import RunStore, mint_task

    store = RunStore(config.db_path)
    try:
        landed = {}
        for name in ("a", "b"):
            repo = tmp_path / name
            (repo / ".git").mkdir(parents=True)
            task = mint_task(workspace=tmp_path / f"w{name}", instructions=name)
            store.create_run(task, repo=repo, ref=None, execution_mode="sandbox")
            store.transition(task.task_id, "completed")
            _landed_on(store, task.task_id, "skep/maintain")
            landed[name] = task.task_id
        chat = store.create_chat(title="pr", model=None)
        card = store.add_chat_action(
            chat.chat_id,
            tool="open_pr",
            args={"branch": "skep/maintain", "repo": str(tmp_path / "a")},
        )
    finally:
        store.close()
    client = serve_client(config)
    (action,) = client.get(f"/api/chats/{chat.chat_id}").json()["actions"]
    assert [r["task_id"] for r in action["card"]["runs"]] == [landed["a"]]
    (row,) = [i for i in client.get("/api/review-queue").json()["items"] if i["id"] == card]
    assert row["task_ids"] == [landed["a"]]


def test_an_open_pr_that_opened_nothing_is_a_failed_card_with_its_reason(
    config: SupervisorConfig, repo: Path
) -> None:
    """R5-7 (I8): open_pr answers {opened: false, detail} when no PR was made
    (the empty-remote case is the common one) — the card is recorded failed
    and says why, never "confirmed" on the outer ok alone."""
    from skep.supervisor import RunStore

    from .conftest import git

    git(repo, "branch", "feat")
    client = serve_client(config)
    chat_id = client.post("/api/chats", json={}).json()["chat_id"]
    card = client.post(
        f"/api/chats/{chat_id}/commands",
        json={"tool": "open_pr", "args": {"branch": "feat", "repo": str(repo)}},
    ).json()
    reply = client.post(f"/api/chats/{chat_id}/commands/{card['action_id']}/confirm").json()
    assert reply["ok"] is False
    # Phase 09 R3: a remoteless open_pr names the missing GitHub repo
    assert reply["error"].startswith("the PR was not opened: ")
    assert "point origin at the GitHub repo" in reply["error"]
    assert reply["result"]["opened"] is False
    store = RunStore(config.db_path, readonly=True)
    try:
        record = store.get_chat_action(card["action_id"])
        assert record is not None and record.status == "failed"
    finally:
        store.close()


@pytest.mark.parametrize("repo", ["bad\x00path", "~skep-fixture-no-such-user/worktree"])
def test_a_branch_card_with_an_unresolvable_repo_lists_no_runs_and_stays_deniable(
    config: SupervisorConfig, ollama: FakeOllama, repo: str
) -> None:
    """R6-1: a Queen-proposed open_pr whose repo holds a NUL byte, or names a
    user with no home (R7), never breaks the evidence lookup — the queue, the
    badge and the chat page answer, the card is there with no runs, and it
    can still be denied."""
    from skep.supervisor import RunStore

    client = serve_client(config)
    client.put(
        "/api/llm/config",
        json={"base_url": ollama.base_url, "default_model": "qwen3", "api_key": "sk-fake"},
    )
    chat_id = client.post("/api/chats", json={}).json()["chat_id"]
    store = RunStore(config.db_path)
    try:
        card = store.add_chat_action(
            chat_id, tool="open_pr", args={"branch": "skep/maintain", "repo": repo}
        )
    finally:
        store.close()
    queue = client.get("/api/review-queue")
    assert queue.status_code == 200, queue.text
    (row,) = [i for i in queue.json()["items"] if i["id"] == card]
    assert row["task_ids"] == []
    assert client.get("/api/review-queue", params={"detail": "false"}).status_code == 200
    page = client.get(f"/api/chats/{chat_id}")
    assert page.status_code == 200, page.text
    (action,) = page.json()["actions"]
    assert "runs" not in action["card"]
    denied = client.post(f"/api/chats/{chat_id}/actions/{card}/deny")
    assert denied.status_code == 200, denied.text


def test_a_branch_card_naming_another_spelling_of_the_repo_lists_its_landing(
    config: SupervisorConfig, tmp_path: Path
) -> None:
    """R6-2: the repo is compared by filesystem identity — a card naming an
    alias of the directory a landing was recorded under (a symlink here; a
    case variant on a case-insensitive volume) lists that run."""
    from skep.supervisor import RunStore, mint_task

    real = tmp_path / "MixedCaseRepo"
    (real / ".git").mkdir(parents=True)
    alias = tmp_path / "mixedcaserepo"
    if not alias.exists():  # a case-sensitive volume (Linux CI): a symlinked alias
        alias.symlink_to(real)
    store = RunStore(config.db_path)
    try:
        task = mint_task(workspace=tmp_path / "w", instructions="x")
        store.create_run(task, repo=alias, ref=None, execution_mode="sandbox")
        store.transition(task.task_id, "completed")
        _landed_on(store, task.task_id, "skep/maintain")
        chat = store.create_chat(title="pr", model=None)
        store.add_chat_action(
            chat.chat_id, tool="open_pr", args={"branch": "skep/maintain", "repo": str(real)}
        )
    finally:
        store.close()
    (action,) = serve_client(config).get(f"/api/chats/{chat.chat_id}").json()["actions"]
    assert [r["task_id"] for r in action["card"]["runs"]] == [task.task_id]


def test_display_patch_keeps_utf8_as_is_and_never_lets_a_byte_read_as_text() -> None:
    """R6-3: valid UTF-8 comes back byte-for-byte; otherwise an invalid byte
    is \\xNN and a literal backslash is doubled, so the ASCII text `caf\\xe9`
    and `caf` + byte 0xE9 never display the same; the "\\ No newline"
    marker still starts with a backslash."""
    from skep.supervisor.serve.actions import ESCAPED_NOTE, display_patch

    plain = "diff --git a/n b/n\n@@ -1 +1 @@\n-caf\\xe9\n+café\n\\ No newline at end of file\n"
    assert display_patch(plain.encode()) == plain
    mixed = b"@@ -1 +1 @@\n-caf\\xe9\n+caf\xe9\n\\ No newline at end of file\n"
    shown = display_patch(mixed).splitlines()
    assert shown[0] == ESCAPED_NOTE
    assert shown[2] == "-caf\\\\xe9" and shown[3] == "+caf\\xe9" and shown[2][1:] != shown[3][1:]
    assert shown[4] == "\\\\ No newline at end of file" and shown[4].startswith("\\")


def test_a_patch_adding_a_byte_never_displays_as_one_adding_its_spelling() -> None:
    """R7: two patches that differ only in `caf` + the four characters \\xe9
    versus `caf` + byte 0xE9 never display the same — the escaped one opens
    with a line saying its bytes are escaped, the plain one has no such line."""
    from skep.supervisor.serve.actions import ESCAPED_NOTE, display_patch

    head = b"diff --git a/n b/n\n--- a/n\n+++ b/n\n@@ -0,0 +1 @@\n+caf"
    spelled, byte = display_patch(head + b"\\xe9\n"), display_patch(head + b"\xe9\n")
    assert spelled != byte
    assert spelled == (head + b"\\xe9\n").decode() and ESCAPED_NOTE not in spelled
    assert byte == ESCAPED_NOTE + "\n" + spelled
