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
    **policy: Any,
) -> Path:
    """A due schedule on a project with every gate open except what the
    arguments close: a usable sandbox, the engine's binary, its auth env."""
    from skep.supervisor import scheduler
    from skep.supervisor.sandbox import SandboxAvailability

    from ..fixtures.toy_repo import create_audit_toy_repo

    repo = create_audit_toy_repo(tmp_path / "work").resolve()
    store.add_project_policy(
        project_id="p", name="p", strategy="trusted_local_dev", phase="build",
        policy={**GATES_OPEN, **policy},
    )  # fmt: skip
    store.add_project_binding(project_id="p", binding_kind="repo_path", binding_value=str(repo))
    probe = SandboxAvailability(True, backend="seatbelt")
    if not usable:
        probe = SandboxAvailability(False, "missing_binary", "sandbox-exec not found")
    monkeypatch.setattr(scheduler, "sandbox_availability", lambda backend=None: probe)
    monkeypatch.setattr(
        scheduler,
        "engine_available",
        lambda engine: (binary, "/usr/bin/x" if binary else f"{engine.binary!r} not on PATH"),
    )
    for var in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
        monkeypatch.setenv(var, "k-fake")
    store.add_schedule(
        make_schedule(
            name="nightly", repo=repo, instructions="x", interval_seconds=86400,
            start_at="2026-06-11T00:00:00Z", chat_id=chat_id,
        )
    )  # fmt: skip
    return repo


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
        ("engine_auth", {"binary": False}, "'codex' not on PATH"),
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
    assert "paused" in line and "gate strict_reads:" in line and "--strict-reads" in line
    assert "repeated failures" not in line
