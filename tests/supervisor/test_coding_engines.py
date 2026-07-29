"""v90-F1 (ADR 0047): choosing the coding agent, and the walls that still apply.

The Claude Code / Codex / Aider adapters have been complete since v33 and
unreachable ever since — nothing mapped a name to them. These pin the registry,
the fail-closed resolution, the network merge an external agent needs to work at
all, and the one rule that is stricter for a CLI engine than for skep's own
worker: it may not run without a project-pinned verify_command.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from skep.supervisor.config import SupervisorConfig
from skep.supervisor.engines import (
    BUILTIN_ENGINE,
    CODING_ENGINES,
    engine_available,
    engine_names,
    resolve_engine,
)
from skep.supervisor.policy_resolver import (
    PolicyResolutionError,
    ResolvedRunPolicy,
    resolve_run_policy,
)
from skep.supervisor.store import RunStore


def test_unset_resolves_to_the_builtin_worker() -> None:
    assert resolve_engine(None).name == BUILTIN_ENGINE
    assert resolve_engine("").name == BUILTIN_ENGINE


def test_an_unknown_engine_fails_closed_and_names_the_choices() -> None:
    """v42's lesson: an unregistered name must never fall back silently."""
    with pytest.raises(ValueError) as excinfo:
        resolve_engine("gpt-pilot")
    message = str(excinfo.value)
    assert "gpt-pilot" in message
    for name in engine_names():
        assert name in message


def test_builtin_defers_to_the_configured_worker_command() -> None:
    """`builtin` must NOT hardcode an argv — SKEP_WORKER_CMD, --worker-cmd and
    the test fake worker all override config.worker_command, and an engine that
    replaced it would quietly take that away."""
    assert resolve_engine(BUILTIN_ENGINE).argv == ()
    assert resolve_engine(BUILTIN_ENGINE).external is False


def test_every_external_engine_declares_its_binary() -> None:
    """An engine skep cannot probe is an engine that fails at dispatch with a
    confusing error instead of at doctor with a clear one (v87-F6)."""
    for engine in CODING_ENGINES.values():
        if engine.external:
            assert engine.binary, engine.name
            present, detail = engine_available(engine)
            assert isinstance(present, bool)
            assert detail  # always says what was probed


def _bind(config: SupervisorConfig, repo: Path, policy: dict[str, object]) -> None:
    # execution_mode is resolved (and required) before the engine is, so every
    # binding here sets it — otherwise the resolver raises on that first and the
    # test proves nothing about engines.
    policy = {"default_execution_mode": "workspace", **policy}
    store = RunStore(config.db_path)
    try:
        store.add_project_policy(
            project_id="engine-project",
            name="trusted repo",
            strategy="trusted_local_dev",
            phase="build",
            policy=policy,
        )
        store.add_project_binding(
            project_id="engine-project", binding_kind="repo_path", binding_value=str(repo)
        )
    finally:
        store.close()


def _resolve(config: SupervisorConfig, repo: Path, engine: str | None = None) -> ResolvedRunPolicy:
    store = RunStore(config.db_path)
    try:
        return resolve_run_policy(
            store=store,
            config=config,
            repo=repo,
            caste="coding",
            network=None,
            env_allowlist=None,
            wall_clock_seconds=None,
            max_iterations=None,
            max_actions=None,
            max_provider_calls=None,
            execution_mode=None,
            engine=engine,
        )
    finally:
        store.close()


def test_a_cli_engine_may_not_run_without_a_pinned_verify_command(
    repo: Path, config: SupervisorConfig
) -> None:
    """Its built-in verification is `git diff --check` — whitespace. G10 would
    re-run a check that cannot fail, so the project must say what verification
    means first (I2). This is the one place v88-F4's opt-in is mandatory."""
    _bind(config, repo, {"coding_engine": "claude_code"})
    with pytest.raises(PolicyResolutionError) as excinfo:
        _resolve(config, repo)
    message = str(excinfo.value)
    assert "verify_command" in message
    assert "git diff --check" in message


def test_a_pinned_cli_engine_resolves_and_gets_its_api_host(
    repo: Path, config: SupervisorConfig
) -> None:
    """v19-F2's rule applied to the agent's own provider: an agent that cannot
    reach its API cannot work, and the failure without this is a timeout rather
    than a stated denial (I12)."""
    _bind(
        config,
        repo,
        {"coding_engine": "claude_code", "verify_command": "pytest -q", "default_network": []},
    )
    resolved = _resolve(config, repo)
    assert resolved.coding_engine == "claude_code"
    assert resolved.verify_command == "pytest -q"
    assert "api.anthropic.com" in (resolved.network_resolved or [])


def test_the_builtin_engine_needs_no_pin_and_adds_no_host(
    repo: Path, config: SupervisorConfig
) -> None:
    """The stricter rule is for external agents only — skep's own worker routes
    every action through the capability layer and nominates a real command."""
    _bind(config, repo, {"default_network": []})
    resolved = _resolve(config, repo)
    assert resolved.coding_engine == BUILTIN_ENGINE
    assert resolved.verify_command == ""
    assert "api.anthropic.com" not in (resolved.network_resolved or [])


def test_an_unknown_engine_in_project_policy_is_rejected_at_resolve(
    repo: Path, config: SupervisorConfig
) -> None:
    _bind(config, repo, {"coding_engine": "not-an-engine"})
    with pytest.raises(PolicyResolutionError) as excinfo:
        _resolve(config, repo)
    assert "not-an-engine" in str(excinfo.value)


def test_a_cli_engine_gets_the_env_it_declares(repo: Path, config: SupervisorConfig) -> None:
    """v94-F3: ADR 0047 §3 applied to env — Claude Code's macOS keychain
    credential lookup needs USER/LOGNAME, and the worker env baseline is
    PATH+HOME only, so without the merge the engine dies on
    'Not logged in · Please run /login' (field runs 019f9e9b/019f9e9d)."""
    _bind(config, repo, {"coding_engine": "claude_code", "verify_command": "pytest -q"})
    resolved = _resolve(config, repo)
    assert "USER" in resolved.permissions.env_allowlist
    assert "LOGNAME" in resolved.permissions.env_allowlist


def test_the_builtin_engine_adds_no_env(repo: Path, config: SupervisorConfig) -> None:
    """The merge is engine-declared, not a global widening: skep's own worker
    keeps the bare G2 allowlist."""
    _bind(config, repo, {})
    resolved = _resolve(config, repo)
    assert "USER" not in resolved.permissions.env_allowlist


def test_an_external_engine_always_resolves_to_the_sandbox(
    repo: Path, config: SupervisorConfig
) -> None:
    """v94-F4: an external agent bypasses the capability layer by design — the
    sandbox IS its confinement (ADR 0047). Field run 019f9e9d executed claude
    on the naked host because the trusted_local_dev default is workspace mode;
    the resolver now coerces, and the coerced mode is the shown mode (I8)."""
    _bind(config, repo, {"coding_engine": "claude_code", "verify_command": "pytest -q"})
    resolved = _resolve(config, repo)
    assert resolved.execution_mode == "sandbox"
    # The builtin worker keeps whatever the policy said (workspace here).
    del resolved


def test_a_request_engine_overrides_the_project_policy(
    repo: Path, config: SupervisorConfig
) -> None:
    """v95-F3: the per-dispatch choice lands ABOVE the v90/v94 guard block, so
    the external-engine walls bind it exactly as they bind the policy key —
    forced sandbox (v94-F4) and the API-host merge (v90-F1) included."""
    _bind(config, repo, {"verify_command": "pytest -q", "default_network": []})
    resolved = _resolve(config, repo, engine="claude_code")
    assert resolved.coding_engine == "claude_code"
    assert resolved.execution_mode == "sandbox"
    assert "api.anthropic.com" in (resolved.network_resolved or [])


def test_a_request_engine_still_requires_the_pinned_verify_command(
    repo: Path, config: SupervisorConfig
) -> None:
    """v95-F3 (I2): choosing the engine per-dispatch is not a way around
    v88-F4 — an unpinned project refuses the external engine either way."""
    _bind(config, repo, {})
    with pytest.raises(PolicyResolutionError) as excinfo:
        _resolve(config, repo, engine="claude_code")
    assert "verify_command" in str(excinfo.value)


def test_an_unknown_request_engine_fails_closed_naming_the_request(
    repo: Path, config: SupervisorConfig
) -> None:
    """v95-F3 (I9): the refusal names the request as the thing to fix, not the
    project policy overlay the request bypassed."""
    _bind(config, repo, {})
    with pytest.raises(PolicyResolutionError) as excinfo:
        _resolve(config, repo, engine="not-an-engine")
    message = str(excinfo.value)
    assert "not-an-engine" in message
    assert "engine argument" in message
    assert "policy overlay" not in message


def test_dispatch_refuses_an_external_engine_outside_the_sandbox(
    repo: Path, config: SupervisorConfig
) -> None:
    """v94-F4: the dispatch chokepoint backs the resolver — a resume or caller
    that hands it workspace mode fails closed naming the fix, before any run
    record or worktree exists."""
    from skep.supervisor.dispatch import run_task

    with pytest.raises(ValueError) as excinfo:
        run_task(
            repo,
            "do a thing",
            config=config,
            execution_mode="workspace",
            coding_engine="claude_code",
            verify_command="pytest -q",
        )
    message = str(excinfo.value)
    assert "sandbox" in message


def test_dispatch_refuses_an_external_engine_without_a_usable_sandbox(
    repo: Path, config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v94-F4: with no usable backend the spawner would silently run the agent
    unsandboxed — for an external engine that is the naked host again, so the
    run refuses instead."""
    import skep.supervisor.dispatch as dispatch_module

    monkeypatch.setattr(
        dispatch_module,
        "sandbox_availability",
        lambda backend=None: type("A", (), {"usable": False, "detail": "forced off"})(),
    )
    with pytest.raises(ValueError) as excinfo:
        dispatch_module.run_task(
            repo,
            "do a thing",
            config=config,
            execution_mode="sandbox",
            coding_engine="claude_code",
            verify_command="pytest -q",
        )
    assert "sandbox" in str(excinfo.value)
