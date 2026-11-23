"""v127-F4: a dead worker-provider key fails the tick, not five runs. The
worker probe (doctor's, one real completion) runs for real against a faked
HTTP transport — never a network call from a test."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from skep import status
from skep.supervisor import SupervisorConfig
from skep.supervisor.scheduler import MAX_SCHEDULE_CONSECUTIVE_FAILURES, make_schedule, run_due
from skep.supervisor.store import RunStore

NOW = "2026-06-11T09:00:00Z"
DEAD = {
    "status": "blocked",
    "kind": "auth",
    "http_status": 401,
    "label": "blocked: 401",
    "detail": "m via https://x: 401 from x",
}


@pytest.fixture()
def store(tmp_path: Path) -> Iterator[RunStore]:
    store = RunStore(tmp_path / "supervisor.sqlite3")
    yield store
    store.close()


@pytest.fixture()
def config(tmp_path: Path) -> SupervisorConfig:
    return SupervisorConfig(home=tmp_path / "home" / "supervisor", worker_command=("false",))


def _add(store: RunStore, tmp_path: Path, name: str, worker_kind: str = "coding") -> None:
    store.add_schedule(
        make_schedule(
            name=name,
            repo=tmp_path / "repo",
            instructions="x",
            interval_seconds=86400,
            worker_kind=worker_kind,
            start_at="2026-06-11T00:00:00Z",
        )
    )


def _probe(monkeypatch: pytest.MonkeyPatch, result: dict[str, Any]) -> list[Path]:
    calls: list[Path] = []

    def fake(home: Path, timeout: float) -> dict[str, Any]:
        calls.append(home)
        return result

    monkeypatch.setattr(status, "worker_provider_check", fake)
    return calls


def _provider_home(config: SupervisorConfig, base_url: str = "http://127.0.0.1:11434") -> None:
    """The daemon settings a worker resolves its provider from (no key)."""
    from skep.supervisor.serve.llm import LLM_BASE_URL, LLM_DEFAULT_MODEL, LLM_PROTOCOL

    config.home.mkdir(parents=True, exist_ok=True)
    settings = RunStore(config.home / "supervisor.sqlite3")
    try:
        settings.set_setting(LLM_BASE_URL, base_url)
        settings.set_setting(LLM_DEFAULT_MODEL, "qwen3")
        settings.set_setting(LLM_PROTOCOL, "ollama")
    finally:
        settings.close()


def _http(monkeypatch: pytest.MonkeyPatch, chat: int | Exception) -> list[str]:
    """Fake the provider at the HTTP transport: the model list answers, the
    completion answers ``chat`` (a status) or raises it. Returns the paths hit."""
    import httpx

    from skep.supervisor.serve import llm

    hits: list[str] = []
    monkeypatch.setattr(llm, "_RETRY_DELAY_SECONDS", 0)
    monkeypatch.delenv(llm.SECRET_ENV, raising=False)

    def handle(self: Any, request: httpx.Request) -> httpx.Response:
        hits.append(request.url.path)
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "qwen3"}]})
        if isinstance(chat, Exception):
            raise chat
        if chat == 200:
            return httpx.Response(200, content=b'{"message": {"content": "pong"}}\n')
        return httpx.Response(chat, json={"error": "nope"})

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle)
    return hits


def _dispatch(summary: str | None = None, state: str = "completed") -> tuple[list[str], Any]:
    seen: list[str] = []

    def dispatch(repo: Path, instructions: str, **kwargs: Any) -> Any:
        seen.append(kwargs["worker_kind"])
        record = SimpleNamespace(task_id=f"t-{len(seen)}", state=state, summary=summary)
        return SimpleNamespace(record=record)

    return seen, dispatch


def test_a_failed_probe_dispatches_nothing_and_never_auto_disables(
    store: RunStore, config: SupervisorConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _probe(monkeypatch, DEAD)
    for name in ("a", "b"):
        _add(store, tmp_path, name)
    _add(store, tmp_path, "audit", worker_kind="audit")  # no provider: still runs
    seen, dispatch = _dispatch()
    results = run_due(store=store, config=config, now=NOW, dispatch=dispatch)
    assert seen == ["audit"]
    assert calls == [config.home.parent]  # once per tick, against the skep home
    by_name = {r.name: r for r in results}
    for name in ("a", "b"):
        assert by_name[name].task_id is None
        assert by_name[name].state.startswith("provider_blocked: ")
        assert "401 from x" in by_name[name].state
        health = store.schedule_health(name)
        assert health is not None
        assert health.window_size == 1 and health.success_rate == 0.0  # one event each
        assert health.consecutive_failures == 0
    # A week of dead-key ticks never auto-disables: fixing the key resumes it.
    for day in range(2, MAX_SCHEDULE_CONSECUTIVE_FAILURES + 3):
        run_due(store=store, config=config, now=f"2026-06-{day + 10}T09:00:00Z", dispatch=dispatch)
    schedule = store.get_schedule("a")
    assert schedule is not None and schedule.enabled


def test_no_probe_when_no_provider_schedule_is_due(
    store: RunStore, config: SupervisorConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _probe(monkeypatch, DEAD)
    _add(store, tmp_path, "audit", worker_kind="audit")
    seen, dispatch = _dispatch()
    run_due(store=store, config=config, now=NOW, dispatch=dispatch)
    assert (calls, seen) == ([], ["audit"])


def test_no_provider_configured_is_not_a_skip(
    store: RunStore, config: SupervisorConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ "unavailable" (nothing configured) is not a dead key: a --worker-cmd
    worker may bring its own model, and the run says what it lacks itself."""
    _probe(monkeypatch, {"status": "unavailable", "label": "none", "detail": "none"})
    _add(store, tmp_path, "a")
    seen, dispatch = _dispatch()
    run_due(store=store, config=config, now=NOW, dispatch=dispatch)
    assert seen == ["coding"]


def test_the_first_401_skips_the_rest_of_the_tick(
    store: RunStore, config: SupervisorConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _probe(monkeypatch, {"status": "ready", "label": "ok", "detail": "ok"})
    for name in ("a", "b", "c"):
        _add(store, tmp_path, name)
    seen, dispatch = _dispatch(
        summary="worker failed: provider request failed: 401 from https://ollama.com",
        state="failed",
    )
    results = run_due(store=store, config=config, now=NOW, dispatch=dispatch)
    assert seen == ["coding"]
    first, *rest = results
    assert first.task_id == "t-1"
    for result in rest:
        assert result.task_id is None
        assert result.state.startswith("provider_blocked: ")
        assert "401" in result.state


def test_doctor_names_the_provider_skipped_schedules(tmp_path: Path) -> None:
    home = tmp_path / "skep"
    (home / "supervisor").mkdir(parents=True)
    store = RunStore(home / "supervisor" / "supervisor.sqlite3")
    try:
        _add(store, tmp_path, "nightly")
        store.mark_schedule_ran(
            "nightly",
            ran_at=NOW,
            next_run_at="2026-06-12T09:00:00Z",
            task_id=None,
            state="provider_blocked: worker provider probe failed",
        )
    finally:
        store.close()
    lines = status._repo_registry_advisories(home)
    assert any("nightly" in line and "provider" in line for line in lines)


def test_an_external_engine_schedule_is_not_held_to_skeps_key(
    store: RunStore, config: SupervisorConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """codex authenticates from its own env (ADR 0047): skep's dead key is
    not its key, so the probe never runs and the run dispatches."""
    from skep.supervisor import scheduler

    calls = _probe(monkeypatch, DEAD)
    monkeypatch.setattr(
        scheduler, "run_policy_for_repo", lambda *a, **kw: {"coding_engine": "codex"}
    )
    _add(store, tmp_path, "a")
    seen, dispatch = _dispatch()
    run_due(store=store, config=config, now=NOW, dispatch=dispatch)
    assert (calls, seen) == ([], ["coding"])


def test_a_provider_skipped_one_shot_has_not_fired(
    store: RunStore, config: SupervisorConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _probe(monkeypatch, DEAD)
    store.add_schedule(
        make_schedule(
            name="once",
            repo=tmp_path / "repo",
            instructions="x",
            interval_seconds=86400,
            start_at="2026-06-11T00:00:00Z",
            once=True,
        )
    )
    seen, dispatch = _dispatch()
    run_due(store=store, config=config, now=NOW, dispatch=dispatch)
    schedule = store.get_schedule("once")
    assert seen == [] and schedule is not None and schedule.enabled


def _timeout() -> Exception:
    import httpx

    return httpx.ReadTimeout("timed out")


def _reset() -> Exception:
    import httpx

    return httpx.ConnectError("connection reset by peer")


@pytest.mark.parametrize("status", [401, 403])
def test_a_refused_key_skips_the_tick(
    store: RunStore,
    config: SupervisorConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    """The real probe against a provider that refuses the key: skipped, and
    the skip names the refusal."""
    _provider_home(config)
    hits = _http(monkeypatch, status)
    _add(store, tmp_path, "a")
    seen, dispatch = _dispatch()
    (result,) = run_due(store=store, config=config, now=NOW, dispatch=dispatch)
    assert seen == [] and "/api/chat" in hits
    assert result.state.startswith("provider_blocked: ")
    assert f"{status} from http://127.0.0.1:11434" in result.state


@pytest.mark.parametrize(
    "chat", [503, 429, 500, _timeout(), _reset()], ids=["503", "429", "500", "timeout", "reset"]
)
def test_a_flaky_or_slow_provider_still_dispatches(
    store: RunStore,
    config: SupervisorConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    chat: int | Exception,
) -> None:
    """v127-F4 review: a timeout, 5xx, 429 or reset (a cold local model
    slower than the probe) is not a dead key — the run goes ahead and says
    what happened itself. A weekly local schedule is never skipped forever."""
    _provider_home(config)
    hits = _http(monkeypatch, chat)
    _add(store, tmp_path, "a")
    seen, dispatch = _dispatch()
    run_due(store=store, config=config, now=NOW, dispatch=dispatch)
    assert seen == ["coding"] and "/api/chat" in hits


def test_a_provider_config_error_skips_the_tick(
    store: RunStore, config: SupervisorConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A worker provider no run could reach (LlmPlanError: here a non-HTTP
    endpoint) is a config error the run would fail on too: skipped."""
    _provider_home(config, base_url="ftp://models.example")
    hits = _http(monkeypatch, 200)
    _add(store, tmp_path, "a")
    seen, dispatch = _dispatch()
    (result,) = run_due(store=store, config=config, now=NOW, dispatch=dispatch)
    assert (seen, hits) == ([], [])
    assert result.state.startswith("provider_blocked: ")
    assert "not a valid HTTP URL" in result.state


def test_doctor_does_not_call_a_slow_provider_a_refusal(
    config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    _provider_home(config)
    _http(monkeypatch, _timeout())
    check = status.worker_provider_check(config.home.parent, 5.0)
    assert check["status"] == "blocked" and check["kind"] == "unreachable"
    assert "refused" not in check["label"]
    _http(monkeypatch, 401)
    check = status.worker_provider_check(config.home.parent, 5.0)
    assert (check["kind"], check["http_status"]) == ("auth", 401)
    assert "refused" in check["label"]


def test_a_worker_cmd_that_runs_an_external_agent_is_not_held_to_skeps_key(
    store: RunStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--worker-cmd / SKEP_WORKER_CMD running claude_code's module IS that
    engine (the resolver's ``wrapped``): it authenticates from its own env,
    so skep's key is never probed — and, Phase 04 4.2, it is held to the
    external gates instead: an unbound repo has no strict reads and no pin."""
    import sys

    config = SupervisorConfig(
        home=tmp_path / "home" / "supervisor",
        worker_command=(sys.executable, "-m", "skep.workers.claude_code"),
    )
    calls = _probe(monkeypatch, DEAD)
    _add(store, tmp_path, "a")
    seen, dispatch = _dispatch()
    (result,) = run_due(store=store, config=config, now=NOW, dispatch=dispatch)
    assert (calls, seen) == ([], [])
    assert result.state.startswith("paused: ") and "'claude_code'" in result.state
    assert "strict_reads" in result.state and "verify_pin" in result.state


def test_a_template_schedule_is_judged_on_the_live_template(
    store: RunStore, config: SupervisorConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The schedule row snapshots the caste at creation; the tick runs the
    template as it is now. A template edited from coding to audit no longer
    needs skep's provider, so a dead key does not skip it."""
    from dataclasses import replace

    from skep.supervisor.scheduler import make_template_schedule
    from skep.supervisor.templates import WorkflowTemplate

    calls = _probe(monkeypatch, DEAD)
    coding = WorkflowTemplate(name="tidy", instructions="tidy up", worker_kind="coding")
    store.add_schedule(
        make_template_schedule(
            name="a",
            template=coding,
            params={},
            repo=tmp_path / "repo",
            interval_seconds=86400,
            start_at="2026-06-11T00:00:00Z",
        )
    )
    store.add_template(replace(coding, worker_kind="audit"))
    seen, dispatch = _dispatch()
    run_due(store=store, config=config, now=NOW, dispatch=dispatch)
    assert (calls, seen) == ([], ["audit"])


# Phase 04 4.2 (I1, I2, I4, I12): an unattended run on an external coding
# agent — its own commands never pass the capability layer (ADR 0047) —
# needs all five gates; one that fails any pauses, with the gate named.

GATES_OPEN: dict[str, Any] = {
    "coding_engine": "codex",
    "verify_command": "uv run pytest -q",
    "default_execution_mode": "sandbox",
    "strict_read_confinement": True,
    "auto_dispatch_allowed": True,
}
READY = {"status": "ready", "label": "ok", "detail": "ok"}


def _bound(
    store: RunStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    usable: bool = True,
    binary: bool = True,
    chat_id: str | None = None,
    network: tuple[str, ...] = (),
    template: Any = None,
    **policy: Any,
) -> Path:
    """A due schedule on a project with every gate open except what the
    arguments close: a usable sandbox, the engine's binary on the worker's
    PATH, its auth env. ``template``: the schedule runs it, bound by repo."""
    from skep.supervisor import scheduler
    from skep.supervisor.sandbox import SandboxAvailability
    from skep.supervisor.scheduler import make_template_schedule

    from ..fixtures.toy_repo import create_audit_toy_repo

    repo = create_audit_toy_repo(tmp_path / "work").resolve()
    _fake_binaries(tmp_path, monkeypatch, on_path=binary)
    store.add_project_policy(
        project_id="p", name="p", strategy="trusted_local_dev", phase="build",
        policy={**GATES_OPEN, **policy},
    )  # fmt: skip
    store.add_project_binding(project_id="p", binding_kind="repo_path", binding_value=str(repo))
    probe = SandboxAvailability(True, backend="seatbelt")
    if not usable:
        probe = SandboxAvailability(False, "missing_binary", "sandbox-exec not found")
    monkeypatch.setattr(scheduler, "sandbox_availability", lambda backend=None: probe)
    for var in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
        monkeypatch.setenv(var, "k-fake")
    when: dict[str, Any] = {
        "interval_seconds": 86400,
        "start_at": "2026-06-11T00:00:00Z",
        "chat_id": chat_id,
    }
    if template is not None:
        store.add_template(template)
        store.add_schedule(
            make_template_schedule(name="nightly", template=template, params={}, repo=repo, **when)
        )
    else:
        store.add_schedule(
            make_schedule(name="nightly", repo=repo, instructions="x", network=network, **when)
        )
    return repo


def _fake_binaries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, on_path: bool) -> Path:
    """Every engine binary as an executable stub, on PATH unless ``on_path``
    is False — and the host's own installs never on it."""
    import os

    from skep.supervisor.engines import CODING_ENGINES

    stubs = tmp_path / "bin"
    stubs.mkdir(exist_ok=True)
    for name in {e.binary for e in CODING_ENGINES.values() if e.binary} | {"codex-acp"}:
        (stubs / name).write_text("#!/bin/sh\n")
        (stubs / name).chmod(0o755)
    system = os.pathsep.join(("/usr/bin", "/bin"))
    monkeypatch.setenv("PATH", f"{stubs}{os.pathsep}{system}" if on_path else system)
    # R2 F5: inside a root the strict wall binds (a detected per-user toolchain)
    monkeypatch.setenv("UV_TOOL_DIR", str(stubs))
    return stubs


def _refusals(config: SupervisorConfig) -> list[dict[str, Any]]:
    import json

    path = config.audit_dir / "policy" / "decisions.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()] if path.is_file() else []
    return [row for row in rows if row.get("scope") == "schedule"]


@pytest.mark.parametrize(
    ("engine", "auth"),
    [("codex", "OPENAI_API_KEY"), ("claude_code", "CLAUDE_CODE_OAUTH_TOKEN")],
)
def test_all_five_gates_open_runs_the_external_engine(
    store: RunStore,
    config: SupervisorConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    engine: str,
    auth: str,
) -> None:
    calls = _probe(monkeypatch, DEAD)
    _bound(store, tmp_path, monkeypatch, coding_engine=engine)
    for var in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
        if var != auth:
            monkeypatch.delenv(var)
    captured: dict[str, Any] = {}

    def dispatch(repo: Path, instructions: str, **kwargs: Any) -> Any:
        captured.update(kwargs)
        return SimpleNamespace(record=SimpleNamespace(task_id="t-1", state="completed"))

    (result,) = run_due(store=store, config=config, now=NOW, dispatch=dispatch)
    assert (result.task_id, result.state, calls) == ("t-1", "completed", [])
    assert captured["coding_engine"] == engine and captured["execution_mode"] == "sandbox"
    assert _refusals(config) == []
    schedule = store.get_schedule("nightly")
    assert schedule is not None and schedule.enabled


@pytest.mark.parametrize(
    ("gate", "closed", "says"),
    [
        ("sandbox", {"default_execution_mode": "workspace"}, "--execution-mode sandbox"),
        ("sandbox", {"usable": False}, "sandbox-exec not found"),
        ("strict_reads", {"strict_read_confinement": False}, "--strict-reads"),
        ("verify_pin", {"verify_command": "git diff --check"}, "--verify-command"),
        ("verify_pin", {"verify_command": ""}, "--verify-command"),
        ("engine_auth", {"binary": False}, "'codex' is not on the worker's PATH"),
        ("engine_auth", {"auth": False}, "OPENAI_API_KEY"),
        ("landing", {"auto_apply_branch": "main"}, "skep/<slug>"),
    ],
)
def test_each_closed_gate_pauses_the_schedule_and_names_itself(
    store: RunStore,
    config: SupervisorConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    gate: str,
    closed: dict[str, Any],
    says: str,
) -> None:
    """Fail closed: no run, the schedule paused (not re-tried each tick), the
    gate and its fix named, and one decisions.jsonl row carrying the blame."""
    closed = dict(closed)
    auth = closed.pop("auth", True)
    landing = closed.pop("auto_apply_branch", None)
    _bound(store, tmp_path, monkeypatch, **closed)
    if not auth:
        monkeypatch.delenv("OPENAI_API_KEY")
    if landing is not None:  # the policy validator refuses it on write; a stale row is not
        store._conn.execute(
            "UPDATE project_policies SET policy_json = ?",
            (__import__("json").dumps({**GATES_OPEN, "auto_apply_branch": landing}),),
        )
        store._conn.commit()
    seen, dispatch = _dispatch()
    (result,) = run_due(store=store, config=config, now=NOW, dispatch=dispatch)
    assert seen == [] and result.task_id is None
    assert result.state.startswith("paused: ") and f"gate {gate}:" in result.state
    assert says in result.state
    schedule = store.get_schedule("nightly")
    health = store.schedule_health("nightly")
    assert schedule is not None and not schedule.enabled
    assert health is not None and health.disabled_reason == result.state
    (row,) = _refusals(config)
    assert (row["verdict"], row["rule_id"], row["decided_by"]) == (
        "deny", f"schedule.{gate}", f"schedule/{gate}"
    )  # fmt: skip
    assert (row["actor"], row["engine"], row["caste"], row["schedule"]) == (
        "schedule:nightly", "codex", "coding", "nightly"
    )  # fmt: skip
    assert "task_id" not in row  # no run was minted (I8)
    # paused, so the next tick does not even look at it
    assert run_due(store=store, config=config, now="2026-06-12T09:00:00Z", dispatch=dispatch) == []


def test_a_codex_plan_login_opens_the_auth_gate(
    store: RunStore, config: SupervisorConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ES-F122: the only login that reaches the sandbox is the opted-in plan
    login — valid for the run's budget, it passes with no API key."""
    from skep.supervisor import codex_login

    _bound(store, tmp_path, monkeypatch, codex_chatgpt_login=True)
    monkeypatch.delenv("OPENAI_API_KEY")
    monkeypatch.setattr(
        codex_login, "login_status", lambda min_seconds=0: codex_login.LoginStatus(True, "plus")
    )
    seen, dispatch = _dispatch()
    run_due(store=store, config=config, now=NOW, dispatch=dispatch)
    assert seen == ["coding"]


def test_an_acp_engine_is_held_to_the_same_gates(
    store: RunStore, config: SupervisorConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate keys on ``external`` and the engine's own auth env, so an
    engine registered later (4A's acp:* adapters) is covered unchanged."""
    from dataclasses import replace

    from skep.supervisor import engines

    acp = replace(engines.CODING_ENGINES["codex"], name="acp:codex-acp", binary="codex-acp")
    monkeypatch.setitem(engines.CODING_ENGINES, "acp:codex-acp", acp)
    _bound(
        store, tmp_path, monkeypatch, coding_engine="acp:codex-acp", strict_read_confinement=False
    )
    seen, dispatch = _dispatch()
    (result,) = run_due(store=store, config=config, now=NOW, dispatch=dispatch)
    assert seen == [] and "'acp:codex-acp'" in result.state and "gate strict_reads:" in result.state
    (row,) = _refusals(config)
    assert row["engine"] == "acp:codex-acp"


def test_a_builtin_schedule_is_not_held_to_the_external_gates(
    store: RunStore, config: SupervisorConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """skep's own worker passes the capability layer on every action: no
    strict reads, no pin, no sandbox — it still runs, as before."""
    _probe(monkeypatch, READY)
    _bound(
        store, tmp_path, monkeypatch, usable=False, coding_engine="builtin",
        strict_read_confinement=False, verify_command="",
    )  # fmt: skip
    seen, dispatch = _dispatch()
    run_due(store=store, config=config, now=NOW, dispatch=dispatch)
    assert seen == ["coding"] and _refusals(config) == []


@pytest.mark.parametrize("chat", ["untrusted", "gone"])
def test_an_untrusted_or_unknown_chat_taint_refuses_the_unattended_run(
    store: RunStore,
    config: SupervisorConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    chat: str,
) -> None:
    """ADR 0059 D5: untrusted → a dispatch asks; nobody is there to answer,
    so the tick refuses — on the final dispatch path, not as metadata only.
    A chat that is gone counts as both bits (get_chat_taint fails closed)."""
    _probe(monkeypatch, READY)
    chat_id = "gone"
    if chat == "untrusted":
        chat_id = store.create_chat(title="c", model=None).chat_id
        store.add_chat_taint(chat_id, untrusted=7)
    _bound(store, tmp_path, monkeypatch, chat_id=chat_id)  # codex, every gate open
    seen, dispatch = _dispatch()
    (result,) = run_due(store=store, config=config, now=NOW, dispatch=dispatch)
    assert seen == [] and result.task_id is None
    assert result.state.startswith("policy_blocked: ") and "untrusted" in result.state
    assert ("message 7" if chat == "untrusted" else "unknown") in result.state
    (row,) = _refusals(config)
    assert (row["rule_id"], row["chat"], row["caste"], row["engine"]) == (
        "schedule.untrusted_taint", chat_id, "coding", "codex"
    )  # fmt: skip
    assert "untrusted" in row["taint"]


def test_an_untrusted_chat_still_dispatches_a_builtin_tick(
    store: RunStore, config: SupervisorConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Master plan trap 6: the refusal is for EXTERNAL engines. A run's own
    notice marks its chat untrusted, so refusing builtin ticks would stop
    every chat-bound schedule after its first run; a builtin tick dispatches
    with the bits attached (v126-F11)."""
    _probe(monkeypatch, READY)
    chat_id = store.create_chat(title="c", model=None).chat_id
    store.add_chat_taint(chat_id, untrusted=7)
    _bound(store, tmp_path, monkeypatch, coding_engine="builtin", chat_id=chat_id)
    seen, dispatch = _dispatch()
    run_due(store=store, config=config, now=NOW, dispatch=dispatch)
    assert seen == ["coding"] and _refusals(config) == []


def test_a_private_only_chat_still_runs(
    store: RunStore, config: SupervisorConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mail chat that only read private data is not an injection risk on
    its own: the private bit alone never stops a scheduled run."""
    _probe(monkeypatch, READY)
    chat_id = store.create_chat(title="mail", model=None).chat_id
    store.add_chat_taint(chat_id, private=3)
    _bound(store, tmp_path, monkeypatch, chat_id=chat_id)
    seen, dispatch = _dispatch()
    run_due(store=store, config=config, now=NOW, dispatch=dispatch)
    assert seen == ["coding"] and _refusals(config) == []


def test_doctor_lists_a_paused_external_schedule_with_its_upgrade_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pre-existing external schedule on a project without strict reads
    pauses on its next tick, and doctor says which gate and the fix — not
    "auto-disabled after repeated failures"."""
    config = SupervisorConfig(home=tmp_path / "skep" / "supervisor", worker_command=("false",))
    config.home.mkdir(parents=True)
    store = RunStore(config.db_path)
    try:
        _bound(store, tmp_path, monkeypatch, strict_read_confinement=False)
        run_due(store=store, config=config, now=NOW, dispatch=_dispatch()[1])
    finally:
        store.close()
    lines = status._repo_registry_advisories(config.home.parent)
    (line,) = [line for line in lines if "nightly" in line]
    assert "paused until all five external-engine gates hold" in line
    assert "gate strict_reads:" in line and "--strict-reads" in line
    assert "repeated failures" not in line


# Phase 04 review R1.


def _capture() -> tuple[list[dict[str, Any]], Any]:
    seen: list[dict[str, Any]] = []

    def dispatch(repo: Path, instructions: str, **kwargs: Any) -> Any:
        seen.append(kwargs)
        return SimpleNamespace(record=SimpleNamespace(task_id=f"t-{len(seen)}", state="completed"))

    return seen, dispatch


@pytest.mark.parametrize(("module", "engine"), [("claude_code", "claude_code"), ("codex", "codex")])
@pytest.mark.parametrize("bound", [True, False])
def test_a_worker_cmd_running_an_external_agent_is_refused_from_an_untrusted_chat(
    store: RunStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    module: str,
    engine: str,
    bound: bool,
) -> None:
    """R1 D1: what runs decides. A --worker-cmd running an external agent on
    a builtin project (or an unbound tick) IS that agent — the taint refusal
    and its row read the same effective engine the five gates do."""
    import sys

    from skep.supervisor import scheduler

    _probe(monkeypatch, READY)
    config = SupervisorConfig(
        home=tmp_path / "home" / "supervisor",
        worker_command=(sys.executable, "-m", f"skep.workers.{module}"),
    )
    chat_id = store.create_chat(title="c", model=None).chat_id
    store.add_chat_taint(chat_id, untrusted=7)
    repo = _bound(store, tmp_path, monkeypatch, coding_engine="builtin", chat_id=chat_id)
    if not bound:  # every gate open on an unbound repo: only the engine differs
        store._conn.execute("DELETE FROM project_bindings")
        store._conn.commit()
        open_policy = {**GATES_OPEN, "coding_engine": "builtin"}
        monkeypatch.setattr(scheduler, "run_policy_for_repo", lambda *a, **k: dict(open_policy))
    seen, dispatch = _capture()
    (result,) = run_due(store=store, config=config, now=NOW, dispatch=dispatch)
    assert seen == [] and result.state.startswith("policy_blocked: "), result.state
    (row,) = _refusals(config)
    assert (row["rule_id"], row["engine"], row["repo"]) == (
        "schedule.untrusted_taint", engine, str(repo)
    )  # fmt: skip


def test_a_runs_own_notice_never_refuses_its_schedules_next_tick(
    store: RunStore, config: SupervisorConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R1 D2: a run's finish notice (``worker:<task_id>``) marks the chat
    untrusted for the Queen (ADR 0059 D5) — but that is the schedule's own
    output, so its next tick still runs; anything else that reads into the
    chat after it (a webhook, a tool result) refuses the tick after."""
    from skep.supervisor.serve import run_status

    monkeypatch.setattr(
        run_status, "run_terminal_text", lambda store, task_id, **_: (f"{task_id} done", "info")
    )
    chat_id = store.create_chat(title="c", model=None).chat_id
    _bound(store, tmp_path, monkeypatch, chat_id=chat_id)  # codex, every gate open
    seen, dispatch = _capture()
    run_due(store=store, config=config, now=NOW, dispatch=dispatch)
    assert store.get_chat_taint(chat_id).untrusted is not None  # the Queen still sees it
    store.add_chat_message(chat_id, role="user", content="nice")  # words that set no bit
    store.add_chat_message(chat_id, role="assistant", content="thanks")
    (second,) = run_due(store=store, config=config, now="2026-06-12T09:00:00Z", dispatch=dispatch)
    assert (second.task_id, len(seen)) == ("t-2", 2) and _refusals(config) == []
    assert seen[1]["dispatch_decision"].constraints["chat_taint"] == ["untrusted"]
    store.add_chat_message(chat_id, role="user", content="hi", provenance="webhook:w")
    (third,) = run_due(store=store, config=config, now="2026-06-13T09:00:00Z", dispatch=dispatch)
    assert len(seen) == 2 and third.state.startswith("policy_blocked: ")
    (row,) = _refusals(config)
    assert row["rule_id"] == "schedule.untrusted_taint"


@pytest.mark.parametrize("foreign", ["tool", "other_run", "inherited", "unrecorded"])
def test_an_untrusted_bit_from_anything_but_the_schedules_own_runs_still_refuses(
    store: RunStore,
    config: SupervisorConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    foreign: str,
) -> None:
    """R1 D2, fail closed: a tool row after the notice that read outside
    content, another run's notice, a bit set by a row that is not in this
    chat, or one whose setters were never recorded (R2 F4)."""
    chat_id = store.create_chat(title="c", model=None).chat_id
    _bound(store, tmp_path, monkeypatch, chat_id=chat_id)
    store.record_schedule_outcome("nightly", task_id="t-0", state="completed")
    if foreign == "inherited":
        store.add_chat_taint(chat_id, untrusted=10_000)
    else:
        store.add_chat_message(chat_id, role="user", content="t-0 done", provenance="worker:t-0")
    if foreign == "tool":  # serve/taint.py marks the row that read outside content
        tool = store.add_chat_message(chat_id, role="tool", content="{}", tool_name="web_fetch")
        store.add_chat_taint(chat_id, untrusted=tool)
    if foreign == "unrecorded":  # a bit set before its setters were recorded
        store._conn.execute(
            "UPDATE chats SET taint = ? WHERE chat_id = ?",
            (__import__("json").dumps({"untrusted": store.get_chat_taint(chat_id).untrusted}),
             chat_id),
        )  # fmt: skip
        store._conn.commit()
    if foreign == "other_run":
        store.add_chat_message(chat_id, role="user", content="x", provenance="worker:t-9")
    seen, dispatch = _capture()
    (result,) = run_due(store=store, config=config, now=NOW, dispatch=dispatch)
    assert seen == [] and result.state.startswith("policy_blocked: ")


def test_an_allow_all_egress_pauses_the_external_tick_at_the_sandbox_gate(
    store: RunStore, config: SupervisorConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R1 D3 (gate a, I12): the sandbox gate is judged on the profile the run
    would launch with. ``*`` egress takes no filtering proxy, so the files
    are walled and the network is not — that is not a confined launch."""
    _bound(store, tmp_path, monkeypatch, network=("*",))
    seen, dispatch = _capture()
    (result,) = run_due(store=store, config=config, now=NOW, dispatch=dispatch)
    assert seen == [] and result.state.startswith("paused: ")
    assert "gate sandbox:" in result.state and "egress" in result.state
    (row,) = _refusals(config)
    assert row["rule_id"] == "schedule.sandbox"


def test_the_engines_binary_must_be_on_the_workers_path_not_only_skeps(
    store: RunStore, config: SupervisorConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R1 D4: the worker's PATH drops skep's own venv (build_worker_env), so
    a binary only there is found by the host and never by the launch."""
    import os
    import sys

    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "codex").write_text("#!/bin/sh\n")
    (venv / "bin" / "codex").chmod(0o755)
    _bound(store, tmp_path, monkeypatch, binary=False)
    monkeypatch.setattr(sys, "prefix", str(venv))
    monkeypatch.setattr(sys, "base_prefix", str(tmp_path / "python"))
    monkeypatch.setenv("PATH", f"{venv / 'bin'}{os.pathsep}{os.environ['PATH']}")
    seen, dispatch = _capture()
    (result,) = run_due(store=store, config=config, now=NOW, dispatch=dispatch)
    assert seen == [] and "gate engine_auth:" in result.state
    assert "'codex' is not on the worker's PATH" in result.state


def test_a_plan_login_is_judged_against_the_ticks_own_budget(
    store: RunStore, config: SupervisorConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R1 D4: a template's budget, not the project's default, is what the
    run must hold the plan login for (run_task re-checks the same)."""
    from skep.supervisor import codex_login
    from skep.supervisor.templates import WorkflowTemplate

    asked: list[float] = []

    def status(min_seconds: float = 0) -> Any:
        asked.append(min_seconds)
        problem = "the login expires sooner" if min_seconds > 3600 else None
        return codex_login.LoginStatus(True, "plus", problem=problem)

    monkeypatch.setattr(codex_login, "login_status", status)
    long = WorkflowTemplate(name="long", instructions="x", wall_clock_seconds=7200)
    _bound(store, tmp_path, monkeypatch, codex_chatgpt_login=True, template=long)
    monkeypatch.delenv("OPENAI_API_KEY")
    seen, dispatch = _capture()
    (result,) = run_due(store=store, config=config, now=NOW, dispatch=dispatch)
    assert seen == [] and "gate engine_auth:" in result.state and 7200 in asked


def test_a_sandbox_switched_off_names_itself_not_a_missing_backend(
    store: RunStore, config: SupervisorConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R1 D5: config.sandbox off is its own reason — never "(None: None)"."""
    from dataclasses import replace

    _bound(store, tmp_path, monkeypatch)
    seen, dispatch = _capture()
    off = replace(config, sandbox=False)
    (result,) = run_due(store=store, config=off, now=NOW, dispatch=dispatch)
    assert seen == [] and "gate sandbox:" in result.state
    assert "None" not in result.state and "sandbox is switched off" in result.state


def test_re_enabling_a_paused_schedule_clears_its_gate_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R1 D6: enable clears ``disabled_reason``, so a later manual disable
    never shows doctor a stale gate it no longer fails."""
    config = SupervisorConfig(home=tmp_path / "skep" / "supervisor", worker_command=("false",))
    config.home.mkdir(parents=True)
    store = RunStore(config.db_path)
    try:
        _bound(store, tmp_path, monkeypatch, strict_read_confinement=False)
        run_due(store=store, config=config, now=NOW, dispatch=_capture()[1])
        store.set_schedule_enabled("nightly", enabled=True)
        health = store.schedule_health("nightly")
        assert health is not None and health.disabled_reason is None
        store.set_schedule_enabled("nightly", enabled=False)
    finally:
        store.close()
    lines = status._repo_registry_advisories(config.home.parent)
    assert not [line for line in lines if "nightly" in line]


def test_a_chat_bound_tick_hands_its_chat_to_the_run(
    store: RunStore, config: SupervisorConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Phase 04 A10 (ADR 0058 D3): a scheduled ACP run files its cards in the
    chat that made the schedule, not a fallback chat the operator never sees."""
    _probe(monkeypatch, READY)
    chat_id = store.create_chat(title="c", model=None).chat_id
    _bound(store, tmp_path, monkeypatch, chat_id=chat_id)
    handed: list[Any] = []

    def dispatch(repo: Path, instructions: str, **kwargs: Any) -> Any:
        handed.append(kwargs.get("chat_id"))
        return SimpleNamespace(
            record=SimpleNamespace(task_id="t-1", state="completed", summary=None)
        )

    run_due(store=store, config=config, now=NOW, dispatch=dispatch)
    assert handed == [chat_id]


# Phase 04 review R2.


def _during_resolution(monkeypatch: pytest.MonkeyPatch, act: Any) -> None:
    """Run ``act`` inside the tick's policy resolution — between the early
    engine lookup and the final gates."""
    from skep.supervisor import scheduler
    from skep.supervisor.policy_resolver import resolve_run_policy as real

    def resolve(*args: Any, **kwargs: Any) -> Any:
        act()
        return real(*args, **kwargs)

    monkeypatch.setattr(scheduler, "resolve_run_policy", resolve)


@pytest.mark.parametrize("engine", ["codex", "builtin"])
def test_the_taint_is_read_at_the_last_moment_before_dispatch(
    store: RunStore,
    config: SupervisorConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    engine: str,
) -> None:
    """R2 F1 (I6): an outside row that lands while the tick resolves its
    policy refuses the external tick — and the one snapshot that judged it
    is the one a builtin tick's run carries."""
    _probe(monkeypatch, READY)
    chat_id = store.create_chat(title="c", model=None).chat_id
    _bound(store, tmp_path, monkeypatch, coding_engine=engine, chat_id=chat_id)
    _during_resolution(
        monkeypatch,
        lambda: store.add_chat_message(chat_id, role="user", content="hi", provenance="webhook:w"),
    )
    seen, dispatch = _capture()
    (result,) = run_due(store=store, config=config, now=NOW, dispatch=dispatch)
    if engine == "builtin":
        (run,) = seen
        assert run["dispatch_decision"].constraints["chat_taint"] == ["untrusted"]
        return
    assert seen == [] and result.state.startswith("policy_blocked: ")
    (row,) = _refusals(config)
    assert row["rule_id"] == "schedule.untrusted_taint"


@pytest.mark.parametrize("template", [False, True])
def test_the_gates_judge_the_engine_the_resolved_launch_runs(
    store: RunStore,
    config: SupervisorConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    template: bool,
) -> None:
    """R2 F2 (I2, I12): a project switched from builtin to an external
    engine while the tick resolves is gated as the engine it dispatches —
    never waved through on the earlier lookup."""
    import json

    from skep.supervisor.templates import WorkflowTemplate

    _probe(monkeypatch, READY)
    nightly = WorkflowTemplate(name="n", instructions="x") if template else None
    _bound(
        store, tmp_path, monkeypatch, coding_engine="builtin", strict_read_confinement=False,
        template=nightly,
    )  # fmt: skip
    flipped = json.dumps({**GATES_OPEN, "strict_read_confinement": False})

    def flip() -> None:
        store._conn.execute("UPDATE project_policies SET policy_json = ?", (flipped,))
        store._conn.commit()

    _during_resolution(monkeypatch, flip)
    seen, dispatch = _capture()
    (result,) = run_due(store=store, config=config, now=NOW, dispatch=dispatch)
    assert seen == [] and result.state.startswith("paused: ")
    assert "gate strict_reads:" in result.state
    (row,) = _refusals(config)
    assert (row["rule_id"], row["engine"]) == ("schedule.strict_reads", "codex")


def test_an_unregistered_acp_adapter_pauses_at_the_auth_gate(
    store: RunStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R2 F3 (I6, I9): an ACP adapter no engine entry declares has no binary
    or auth env skep could check — not builtin's empty ones. Unattended, it
    pauses at engine_auth, naming the adapter it would have run."""
    import sys

    config = SupervisorConfig(
        home=tmp_path / "home" / "supervisor",
        worker_command=(sys.executable, "-m", "skep.workers.acp", "npx", "-y", "@x/odd-acp"),
    )
    _bound(store, tmp_path, monkeypatch, coding_engine="builtin")
    seen, dispatch = _capture()
    (result,) = run_due(store=store, config=config, now=NOW, dispatch=dispatch)
    assert seen == [] and result.state.startswith("paused: ")
    assert "gate engine_auth:" in result.state and "@x/odd-acp" in result.state
    (row,) = _refusals(config)
    assert (row["rule_id"], row["engine"]) == ("schedule.engine_auth", "acp")


@pytest.mark.parametrize(
    ("read", "refused"), [("private", False), (None, False), ("untrusted", True)]
)
def test_only_a_row_that_set_untrusted_after_a_self_notice_refuses(
    store: RunStore,
    config: SupervisorConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    read: str | None,
    refused: bool,
) -> None:
    """R2 F4: the rows that CONTRIBUTE ``untrusted`` are recorded even while
    the sticky bit is on, so a private-only read or a denied tool (no bit)
    after the schedule's own notice no longer refuses its tick; a tool row
    that read outside content still does."""
    chat_id = store.create_chat(title="c", model=None).chat_id
    _bound(store, tmp_path, monkeypatch, chat_id=chat_id)
    store.record_schedule_outcome("nightly", task_id="t-0", state="completed")
    store.add_chat_message(chat_id, role="user", content="t-0 done", provenance="worker:t-0")
    tool = store.add_chat_message(chat_id, role="tool", content="{}", tool_name="read_file")
    if read is not None:
        store.add_chat_taint(chat_id, **{read: tool})
    seen, dispatch = _capture()
    (result,) = run_due(store=store, config=config, now=NOW, dispatch=dispatch)
    if not refused:
        assert len(seen) == 1 and _refusals(config) == []
        return
    assert seen == [] and f"message {tool}" in result.state


@pytest.mark.parametrize("listed", [False, True])
def test_the_binary_must_sit_inside_the_strict_walls_roots(
    store: RunStore,
    config: SupervisorConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    listed: bool,
) -> None:
    """R2 F5 (I9, I12): on the worker's PATH is not enough — the strict
    launch binds only the system dirs, the detected toolchains and the
    project's extra_read_paths, so a binary elsewhere could never start."""
    import json
    import os

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "codex").write_text("#!/bin/sh\n")
    (elsewhere / "codex").chmod(0o755)
    _bound(store, tmp_path, monkeypatch)
    if listed:  # the validator's home is pytest's basetemp, so the row is written as stored
        policy = {**GATES_OPEN, "extra_read_paths": [str(elsewhere)]}
        store._conn.execute("UPDATE project_policies SET policy_json = ?", (json.dumps(policy),))
        store._conn.commit()
    monkeypatch.setenv("PATH", f"{elsewhere}{os.pathsep}{os.environ['PATH']}")
    seen, dispatch = _capture()
    (result,) = run_due(store=store, config=config, now=NOW, dispatch=dispatch)
    if listed:
        assert len(seen) == 1 and _refusals(config) == []
        return
    assert seen == [] and "gate engine_auth:" in result.state
    assert "strict read wall" in result.state and str(elsewhere) in result.state
