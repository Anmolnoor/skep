"""first-run O3 (ADR 0056 D7/D8): `skep onboard` — five resumable steps.

Temp homes only; the provider is ``mock`` (no call) or the FakeOllama
stand-in on localhost."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from skep.supervisor import RunStore, sandbox
from skep.supervisor.cli_cmds import build_config
from skep.supervisor.onboard import (
    baseline_candidate,
    onboard_model,
    onboard_role,
    onboard_sandbox,
    onboard_status,
    onboard_template,
    onboard_workdir,
)
from skep.supervisor.policy_baseline import Ok, baseline_path, verify
from skep.supervisor.projects import list_projects
from skep.supervisor.serve.llm import LLM_BASE_URL, LLM_DEFAULT_MODEL
from skep.supervisor.serve.settings import SANDBOX_BACKEND, ConfigHolder

from .fake_ollama import FakeOllama


@contextmanager
def _holder(home: Path) -> Iterator[tuple[ConfigHolder, RunStore]]:
    config = build_config(home, None)
    store = RunStore(config.db_path)
    try:
        yield ConfigHolder(config, store), store
    finally:
        store.close()


def _unlock(home: Path) -> None:
    path = baseline_path(home / "supervisor")
    path.chmod(0o644)
    data = json.loads(path.read_text())
    data["rules"] = data["rules"][1:]
    path.write_text(json.dumps(data))


def test_the_five_steps_leave_a_governed_home(tmp_path: Path) -> None:
    home, work = tmp_path / "home", tmp_path / "work"
    with _holder(home) as (holder, store):
        assert onboard_status(home)["next"] == "model"
        onboard_model(holder, store, provider="mock", model="stub")
        workdir = onboard_workdir(holder, store, path=str(work))
        sandboxed = onboard_sandbox(holder, store, backend="none")
        onboard_template(holder, template="builder", approved_by="tester")
        onboard_role(holder, role="builder")
        assert store.get_setting(SANDBOX_BACKEND) == "none"
        assert [p.project_id for p in list_projects(store)] == ["work"]

    status = onboard_status(home)
    assert status["next"] is None and status["done"]
    assert workdir["created"] and (work / ".git").is_dir()
    assert "builder` asks on every write" in sandboxed["rings"]
    state = verify(home / "supervisor")
    assert isinstance(state, Ok) and state.layer.template == "builder"
    rules = {rule.rule_id: rule.verdict for rule in state.layer.rules}
    # D8: no sandbox ring → in-workspace writes ask instead of being allowed
    assert rules["fs-workspace-unsandboxed"] == "require_approval"
    assert rules["coding-workspace-unsandboxed"] == "require_approval"
    assert "fs-workspace" not in rules
    first_run = json.loads((home / "first_run.json").read_text())
    assert set(first_run) == {"started_at", "turns_seen", "verdicts_seen", "dismissed", "role"}
    assert (first_run["turns_seen"], first_run["dismissed"], first_run["role"]) == (
        0,
        False,
        "builder",
    )


def test_a_sandboxed_candidate_is_the_template_unchanged() -> None:
    assert baseline_candidate("builder", "auto") == baseline_candidate("builder", None)
    rules = {r.rule_id: r.verdict for r in baseline_candidate("builder", "auto").rules}
    assert rules["fs-workspace"] == "allow"


def test_a_tampered_baseline_sends_onboard_back_to_the_template_step(tmp_path: Path) -> None:
    home = tmp_path / "home"
    with _holder(home) as (holder, store):
        onboard_model(holder, store, provider="mock", model="stub")
        onboard_workdir(holder, store, path=str(tmp_path / "w"))
        onboard_sandbox(holder, store, backend="none")
        onboard_template(holder, template="assistant", approved_by="tester")
        assert onboard_status(home)["next"] == "role"
        _unlock(home)
        assert onboard_status(home)["next"] == "template"
        # re-running the step re-locks (the reapprove path), and resumes
        onboard_template(holder, template="assistant", approved_by="tester")
        assert onboard_status(home)["next"] == "role"


def test_the_sandbox_step_refuses_what_this_host_cannot_run(tmp_path: Path) -> None:
    def unusable(backend: str | None = None) -> sandbox.SandboxAvailability:
        return sandbox.SandboxAvailability(False, "missing_binary", "not here")

    with _holder(tmp_path / "home") as (holder, store):
        with pytest.raises(ValueError, match="no docker backend"):
            onboard_sandbox(holder, store, backend="docker")
        with pytest.raises(ValueError, match="podman is unusable here"):
            onboard_sandbox(holder, store, backend="podman", probe=unusable)
        with pytest.raises(ValueError, match="must be one of"):
            onboard_sandbox(holder, store, backend="firecracker")
        assert store.get_setting(SANDBOX_BACKEND) is None


def test_the_model_step_makes_a_test_call_and_says_where_prompts_go(tmp_path: Path) -> None:
    fake = FakeOllama(models=["llama3.2"]).start()
    try:
        fake.script_reply("pong")
        with _holder(tmp_path / "home") as (holder, store):
            record = onboard_model(
                holder, store, provider="ollama", model="llama3.2", endpoint=fake.base_url
            )
            assert store.get_setting(LLM_BASE_URL) == fake.base_url
            assert store.get_setting(LLM_DEFAULT_MODEL) == "llama3.2"
        assert record["egress"] == "LOCAL (Ollama (local)) — nothing leaves this machine"
        assert record["test"].startswith("provider ready")
        # no scripted reply left → the completion probe fails → no record
        with (
            _holder(tmp_path / "home2") as (holder, store),
            pytest.raises(ValueError, match="test call failed"),
        ):
            onboard_model(
                holder, store, provider="ollama", model="llama3.2", endpoint=fake.base_url
            )
        assert onboard_status(tmp_path / "home2")["next"] == "model"
    finally:
        fake.stop()
