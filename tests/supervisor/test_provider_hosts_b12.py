"""B12 (ADR 0059 D3, V6a): a role chat on a locked-baseline home dispatches a
builtin coding run that can reach its own provider — with and without an
explicit ``network`` — from chat, REST and the CLI; an external engine gets
only its own host (B140); no request can name a provider host."""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path
from typing import Any

import pytest

from skep.cli import main
from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor.serve.actions import submit_run
from skep.supervisor.serve.llm import LLM_BASE_URL
from skep.supervisor.serve.settings import ConfigHolder
from skep.worker_contract import CodingWorkerTask, Permissions
from skep.workers.llm_plan import _ensure_network_allowed

from .conftest import FAKE_WORKER, serve_client, wait_terminal
from .test_role_policy import _home

PROVIDERS = [
    ("http://localhost:11434", "localhost:11434"),
    ("https://ollama.com", "ollama.com:443"),
]


@pytest.fixture
def config(tmp_path: Path, repo: Path, capsys: pytest.CaptureFixture[str]) -> SupervisorConfig:
    """A bound trusted_local_dev repo with the builder baseline locked."""
    return _home(tmp_path, repo, capsys)


def _set_provider(config: SupervisorConfig, url: str) -> None:
    store = RunStore(config.db_path)
    try:
        store.set_setting(LLM_BASE_URL, url)
    finally:
        store.close()


class _Runner:
    def __init__(self) -> None:
        self.submitted: dict[str, Any] = {}

    def submit(self, repo: Path, instructions: str, **kwargs: Any) -> str:
        self.submitted = kwargs
        return "task-1"


def _submit(config: SupervisorConfig, repo: Path, **kwargs: Any) -> dict[str, Any]:
    store = RunStore(config.db_path)
    runner = _Runner()
    try:
        submit_run(
            ConfigHolder(config, store),
            runner,  # type: ignore[arg-type]
            store,
            repo=str(repo),
            instructions="fix hello.py",
            **kwargs,
        )
    finally:
        store.close()
    return runner.submitted


def _reaches(url: str, permissions: Permissions) -> None:
    """The worker's own provider check (llm_plan) passes."""
    _ensure_network_allowed(url, permissions.network, permissions.provider_hosts)


@pytest.mark.parametrize(("url", "entry"), PROVIDERS)
@pytest.mark.parametrize("network", [None, ["localhost"]], ids=["default", "explicit"])
@pytest.mark.parametrize(
    "who",
    [{"delegation_chain": ["queen@builder"]}, {"role": "builder"}],
    ids=["role-chat", "worker-role"],
)
def test_b12_a_delegated_coding_run_keeps_its_provider(
    config: SupervisorConfig,
    repo: Path,
    url: str,
    entry: str,
    network: list[str] | None,
    who: dict[str, Any],
) -> None:
    _set_provider(config, url)
    submitted = _submit(config, repo, network=network, **who)
    permissions = submitted["permissions"]
    assert permissions.provider_hosts == [entry]
    _reaches(url, permissions)
    # skep's own endpoint is no host grant the chain dropped (I8: the note
    # names only what really was dropped).
    detail = submitted["dispatch_decision"].detail or ""
    assert "network.connect localhost" not in detail
    assert "network.connect ollama.com" not in detail


def test_the_provider_rides_provider_hosts_not_network(
    config: SupervisorConfig, repo: Path
) -> None:
    """The registry merge (v23-F5) still lands in network; the provider does not."""
    _set_provider(config, "https://ollama.com")
    permissions = _submit(config, repo)["permissions"]
    assert "ollama.com" not in permissions.network
    assert permissions.provider_hosts == ["ollama.com:443"]
    assert "pypi.org" in permissions.network


def test_a_provider_less_caste_gets_no_provider_host(config: SupervisorConfig, repo: Path) -> None:
    _set_provider(config, "https://ollama.com")
    assert _submit(config, repo, caste="audit")["permissions"].provider_hosts == []


def test_an_external_engine_gets_only_its_own_host(config: SupervisorConfig, repo: Path) -> None:
    """B140: claude_code reaches api.anthropic.com, never the builtin's
    ollama.com."""
    _set_provider(config, "https://ollama.com")
    store = RunStore(config.db_path)
    try:
        store.add_project_policy(
            project_id="app",
            name="app",
            strategy="trusted_local_dev",
            phase="build",
            policy={"verify_command": "true"},
        )
    finally:
        store.close()
    permissions = _submit(config, repo, engine="claude_code")["permissions"]
    assert permissions.provider_hosts == ["api.anthropic.com:443"]
    assert "ollama.com" not in permissions.egress()
    assert not any(e.startswith("ollama.com") for e in permissions.egress())


def _cli_run(config: SupervisorConfig, repo: Path, *extra: str) -> CodingWorkerTask:
    home = ["--home", str(config.home.parent)]
    argv = [*home, "run", str(repo), "Fix. MODE:happy", "--quiet", "--execution-mode", "workspace"]
    worker = shlex.join([sys.executable, str(FAKE_WORKER)])
    assert main([*argv, "--worker-cmd", worker, *extra]) == 0
    store = RunStore(config.db_path)
    try:
        task_id = store.recent_runs(1)[0].task_id
    finally:
        store.close()
    return CodingWorkerTask.model_validate_json(
        (config.audit_dir / task_id / "task.json").read_text(encoding="utf-8")
    )


def test_b12_cli_dispatch_with_a_worker_role_keeps_the_provider(
    config: SupervisorConfig, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _set_provider(config, "http://localhost:11434")
    for extra in (["--role", "builder"], ["--role", "builder", "--network", "localhost"]):
        task = _cli_run(config, repo, *extra)
        assert task.permissions.provider_hosts == ["localhost:11434"]
        _reaches("http://localhost:11434", task.permissions)
    capsys.readouterr()


def test_b12_rest_dispatch_with_a_role_keeps_the_provider_and_ignores_an_injected_one(
    config: SupervisorConfig, repo: Path
) -> None:
    """REST hits B12 too (a worker role narrows), and a request body cannot
    name a provider host: provider_hosts comes from the supervisor's own
    registry only."""
    _set_provider(config, "https://ollama.com")
    with serve_client(config) as client:
        response = client.post(
            "/api/runs",
            json={
                "repo": str(repo),
                "instructions": "Fix the bug. MODE:happy",
                "execution_mode": "workspace",
                "role": "builder",
                "provider_hosts": ["evil.example:443"],
                "permissions": {"provider_hosts": ["evil.example:443"]},
            },
        )
        assert response.status_code in (200, 202), response.text
        task_id = str(response.json()["task_id"])
        wait_terminal(client, task_id)
        policy = client.get(f"/api/repos/{repo}/effective-policy").json()
    envelope = json.loads((config.audit_dir / task_id / "task.json").read_text(encoding="utf-8"))
    assert envelope["permissions"]["provider_hosts"] == ["ollama.com:443"]
    assert "evil.example" not in json.dumps(envelope)
    # I8: the effective-policy view shows the provider apart from the grants.
    assert policy["provider_hosts"] == ["ollama.com:443"]
    assert "ollama.com" not in policy["network"]


def test_run_task_fills_provider_hosts_whatever_the_caller_passed(
    config: SupervisorConfig, repo: Path
) -> None:
    """The one choke point: every face (resume, scheduler, batch, templates)
    reaches run_task, which overwrites provider_hosts from the registry."""
    from skep.supervisor.dispatch import run_task

    _set_provider(config, "http://localhost:11434")
    forged = Permissions(
        read=["workspace"],
        write=["workspace"],
        network=[],
        env_allowlist=[],
        provider_hosts=["evil.example:443"],
    )
    worker = (sys.executable, str(FAKE_WORKER))
    from dataclasses import replace

    outcome = run_task(
        repo,
        "Fix. MODE:happy",
        config=replace(config, worker_command=worker),
        permissions=forged,
        execution_mode="workspace",
    )
    envelope = json.loads(
        (config.audit_dir / outcome.record.task_id / "task.json").read_text(encoding="utf-8")
    )
    assert envelope["permissions"]["provider_hosts"] == ["localhost:11434"]


def test_policy_why_names_the_provider_as_skeps_own_endpoint(
    config: SupervisorConfig, repo: Path
) -> None:
    """I8: `skep policy why network connect <provider>` no longer reads a
    compiled net:<provider> allow; it says the endpoint is a supervisor
    resource the run reaches either way."""
    from skep.supervisor.serve.actions import policy_why

    _set_provider(config, "http://localhost:11434")
    store = RunStore(config.db_path)
    try:
        holder = ConfigHolder(config, store)
        view = policy_why(holder, store, str(repo), "network", "connect", "localhost")
        other = policy_why(holder, store, str(repo), "network", "connect", "pypi.org")
    finally:
        store.close()
    assert "localhost:11434 is skep's own LLM endpoint" in view["provider_host"]
    assert view["run"]["decided_by"] != "trusted_local_dev/net:localhost"
    assert "provider_host" not in other
