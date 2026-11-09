"""first-run O3 (ADR 0056 D7/D8): `skep onboard` — five resumable steps.

Temp homes only; the provider is ``mock`` (no call) or the FakeOllama
stand-in on localhost."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from skep.cli import main
from skep.supervisor import RunStore, sandbox
from skep.supervisor.cli_cmds import build_config
from skep.supervisor.config import SupervisorConfig
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

from .conftest import serve_client
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
        assert onboard_status(home / "supervisor")["next"] == "model"
        onboard_model(holder, store, provider="mock", model="stub")
        workdir = onboard_workdir(holder, store, path=str(work))
        sandboxed = onboard_sandbox(holder, store, backend="none")
        onboard_template(holder, template="builder", approved_by="tester")
        onboard_role(holder, role="builder")
        assert store.get_setting(SANDBOX_BACKEND) == "none"
        assert [p.project_id for p in list_projects(store)] == ["work"]

    status = onboard_status(home / "supervisor")
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
        assert onboard_status(home / "supervisor")["next"] == "role"
        _unlock(home)
        assert onboard_status(home / "supervisor")["next"] == "template"
        # re-running the step re-locks (the reapprove path), and resumes
        onboard_template(holder, template="assistant", approved_by="tester")
        assert onboard_status(home / "supervisor")["next"] == "role"


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
        assert onboard_status(tmp_path / "home2" / "supervisor")["next"] == "model"
    finally:
        fake.stop()


def _answers(monkeypatch: pytest.MonkeyPatch, *replies: str | type[BaseException]) -> list[str]:
    """Script ``input()``; an exception class in the list is raised there."""
    prompts: list[str] = []
    queue = list(replies)

    def fake_input(prompt: str = "") -> str:
        prompts.append(prompt)
        reply = queue.pop(0)
        if isinstance(reply, type):
            raise reply
        return reply

    monkeypatch.setattr("builtins.input", fake_input)
    return prompts


def test_interactive_onboard_resumes_after_an_interrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from skep.supervisor import onboard

    home, work = tmp_path / "home", tmp_path / "work"
    monkeypatch.setattr(onboard, "detect_ollama", lambda *a, **k: None)
    monkeypatch.setattr(
        onboard,
        "detect_sandboxes",
        lambda *a, **k: {
            "backends": {"none": {"usable": True, "detail": "no sandbox ring"}},
            "recommended": "none",
            "docker": {"found": False, "detail": "not on PATH"},
        },
    )
    prompts = _answers(
        monkeypatch,
        *("mock", "stub", "", ""),  # the v37-F3 setup wizard: provider, model, endpoint, key env
        str(work),
        "",  # sandbox: the detected default
        "builder",
        KeyboardInterrupt,  # at the typed yes
    )
    assert main(["--home", str(home), "onboard", "--no-chat"]) == 130
    assert prompts[-1] == "type yes to lock this as the policy baseline: "
    out = capsys.readouterr().out
    assert "policy template: assistant" in out  # all three explain tables shown first
    assert onboard.onboard_status(home / "supervisor")["next"] == "template"

    resume = ["--home", str(home), "onboard", "--template", "builder", "--yes", "--no-chat"]
    assert main(resume) == 0
    out = capsys.readouterr().out
    assert "① model: done — mock stub" in out
    assert f"② work directory: done — {work.resolve()}" in out
    assert "③ sandbox: done — none" in out
    assert "baseline locked: builder" in out
    assert onboard.onboard_status(home / "supervisor")["done"]


def _onboard_flags(home: Path, workdir: Path) -> list[str]:
    """The non-interactive form the docs give, on a stubbed provider."""
    return [
        *("--home", str(home), "onboard", "--provider", "mock", "--model", "stub"),
        *("--workdir", str(workdir), "--sandbox", "none", "--template", "builder"),
        *("--yes", "--no-chat"),
    ]


def test_flag_driven_onboard_then_check_is_ready_with_telemetry_none_last(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The O3 done-criterion: a locked baseline, a registered project, the
    sandbox record and first_run.json; then --check exits 0 and its last
    line is the egress truth's `telemetry: none`."""
    home, work = tmp_path / "home", tmp_path / "work"
    assert main(_onboard_flags(home, work)) == 0
    capsys.readouterr()
    assert isinstance(verify(home / "supervisor"), Ok)
    assert json.loads((home / "onboard.json").read_text())["sandbox"]["backend"] == "none"
    assert json.loads((home / "first_run.json").read_text())["role"] == "builder"
    with _holder(home) as (_, store):
        assert [p.project_id for p in list_projects(store)] == ["work"]

    assert main(["--home", str(home), "onboard", "--check"]) == 0
    out = capsys.readouterr().out
    assert out.rstrip().splitlines()[-1] == "telemetry: none"
    assert "- baseline: ready - builder locked" in out
    assert "- sandbox: ready - none" in out
    assert "model: mock — no call leaves this machine" in out


def test_check_names_a_tampered_baseline_and_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    assert main(_onboard_flags(home, tmp_path / "work")) == 0
    _unlock(home)
    capsys.readouterr()
    assert main(["--home", str(home), "onboard", "--check"]) == 1
    out = capsys.readouterr().out
    assert "- baseline: blocked - baseline_tampered:" in out
    assert "- steps: blocked - pending: template" in out
    assert out.rstrip().splitlines()[-1] == "telemetry: none"


def test_check_on_a_fresh_home_is_not_ready(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--home", str(tmp_path / "home"), "onboard", "--check"]) == 1
    out = capsys.readouterr().out
    assert "- baseline: blocked - none approved — run skep onboard" in out
    assert "- sandbox: blocked - not chosen" in out


def test_the_rest_face_drives_the_same_steps(config: SupervisorConfig, tmp_path: Path) -> None:
    """ADR 0050: the web /setup form posts to these routes; they call the
    step functions, so the CLI sees the same records."""
    client = serve_client(config)
    assert client.get("/api/onboard").json()["next"] == "model"
    post = client.post
    assert post("/api/onboard/model", json={"provider": "mock", "model": "stub"}).status_code == 200
    assert post("/api/onboard/workdir", json={"path": str(tmp_path / "w")}).status_code == 200
    refused = post("/api/onboard/sandbox", json={"backend": "docker"})
    assert refused.status_code == 400 and "no docker backend" in refused.json()["detail"]
    assert post("/api/onboard/sandbox", json={"backend": "none"}).status_code == 200
    preview = client.get("/api/onboard/template", params={"template": "builder"}).json()
    assert "fs-workspace-unsandboxed" in preview["table"] and not preview["locked"]
    locked = post(
        "/api/onboard/template",
        json={"template": "builder", "expect_sha256": preview["sha256"], "approved_by": "web"},
    )
    assert locked.status_code == 200
    assert post("/api/onboard/role", json={"role": "builder"}).status_code == 200
    assert post("/api/onboard/frobnicate", json={}).status_code == 404
    assert client.get("/api/onboard").json()["done"] is True
    check = client.get("/api/onboard/check").json()
    assert check["ready"] is True and check["egress"][-1] == "telemetry: none"
    assert onboard_status(config.home)["done"]


def test_every_documented_onboard_command_parses() -> None:
    """O5's Air smoke runs `skep onboard` from the docs alone — a flag the
    docs name and the parser lacks fails here, not on the Air."""
    import re
    import shlex

    from skep.cli import _parser

    docs = Path(__file__).parents[2] / "docs"
    text = "\n".join((docs / name).read_text() for name in ("onboarding.md", "acp-governor.md"))
    text = text.replace("\\\n", " ").replace("&&", "\n")
    commands = re.findall(r"^\s*(skep onboard[^\n]*)", text, re.MULTILINE)  # code lines
    assert any("--template builder" in command for command in commands)
    for command in commands:
        _parser().parse_args(shlex.split(command)[1:])


def test_a_refused_typed_yes_locks_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    argv = ["--home", str(home), "onboard", "--provider", "mock", "--model", "stub"]
    argv += ["--workdir", str(tmp_path / "w"), "--sandbox", "none", "--template", "operator"]
    _answers(monkeypatch, "no")
    assert main([*argv, "--no-chat"]) == 1
    assert "not approved — nothing was locked" in capsys.readouterr().err
    assert not baseline_path(home / "supervisor").exists()
