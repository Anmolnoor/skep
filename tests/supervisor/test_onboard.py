"""first-run O3 (ADR 0056 D7/D8): `skep onboard` — five resumable steps.

Temp homes only; the provider is ``mock`` (no call) or the FakeOllama
stand-in on localhost."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

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


def _usable(backend: str | None = None) -> sandbox.SandboxAvailability:
    return sandbox.SandboxAvailability(True, None, "stub", backend="seatbelt")


def test_a_sandbox_switch_after_lock_reopens_the_template_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """O3-F1: `--sandbox none` after a sandboxed lock re-shows ④ and re-locks
    the tightened builder — the baseline is what ③ says it is (D8)."""
    from functools import partial

    from skep.supervisor import onboard

    monkeypatch.setattr(onboard, "onboard_sandbox", partial(onboard.onboard_sandbox, probe=_usable))
    home = tmp_path / "home"
    argv = _onboard_flags(home, tmp_path / "work")
    argv[argv.index("none")] = "auto"
    assert main(argv) == 0
    state = verify(home / "supervisor")
    assert isinstance(state, Ok)
    assert {r.rule_id: r.verdict for r in state.layer.rules}["fs-workspace"] == "allow"
    capsys.readouterr()

    assert main(["--home", str(home), "onboard", "--sandbox", "none", "--yes", "--no-chat"]) == 0
    out = capsys.readouterr().out
    assert "④ template: done" not in out
    assert "fs-workspace-unsandboxed" in out  # explain re-shown for the new candidate
    assert "baseline locked: builder" in out
    state = verify(home / "supervisor")
    assert isinstance(state, Ok)
    rules = {r.rule_id: r.verdict for r in state.layer.rules}
    assert "fs-workspace" not in rules and "coding-workspace" not in rules
    assert rules["fs-workspace-unsandboxed"] == "require_approval"
    assert onboard_status(home / "supervisor")["done"]


def test_check_names_a_baseline_the_sandbox_no_longer_matches(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Either direction re-opens ④ (none → auto leaves it stricter, still not
    what onboard shows); nothing is re-locked without the approval path."""
    home = tmp_path / "home"
    assert main(_onboard_flags(home, tmp_path / "work")) == 0
    locked = verify(home / "supervisor")
    with _holder(home) as (holder, store):
        onboard_sandbox(holder, store, backend="auto", probe=_usable)
    status = onboard_status(home / "supervisor")
    assert status["next"] == "template" and status["relock"] == "builder"
    assert verify(home / "supervisor") == locked  # not silently rewritten
    capsys.readouterr()
    assert main(["--home", str(home), "onboard", "--check"]) == 1
    out = capsys.readouterr().out
    assert (
        "- baseline: blocked - builder locked under another sandbox — not what onboard "
        "locks for builder with sandbox auto; run skep onboard to re-approve" in out
    )
    assert "- steps: blocked - pending: template" in out


# --- T1: what O3 promised and nothing pinned ------------------------------------


def _approvals(home: Path) -> list[str]:
    from skep.supervisor.policy_baseline import ledger_path

    rows = [json.loads(line) for line in ledger_path(home / "supervisor").read_text().splitlines()]
    return [row["template"] for row in rows if row["kind"] == "baseline_approved"]


def test_a_flag_that_differs_from_a_record_reruns_only_that_step(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    assert main(_onboard_flags(home, tmp_path / "work")) == 0
    capsys.readouterr()
    # Nothing changed: every step is done and nothing is re-approved.
    assert main(["--home", str(home), "onboard", "--yes", "--no-chat"]) == 0
    out = capsys.readouterr().out
    assert "④ template: done — builder locked" in out and "⑤ role: done — builder" in out
    assert _approvals(home) == ["builder"]
    # A different --template re-runs step ④ alone: the diff, then the re-lock.
    argv = ["--home", str(home), "onboard", "--template", "operator", "--yes", "--no-chat"]
    assert main(argv) == 0
    out = capsys.readouterr().out
    assert "① model: done" in out and "③ sandbox: done — none" in out
    assert "changes against the locked baseline:" in out
    assert "~ template: 'builder' -> 'operator'" in out
    assert "baseline locked: operator" in out
    assert _approvals(home) == ["builder", "operator"]
    state = verify(home / "supervisor")
    assert isinstance(state, Ok) and state.layer.template == "operator"
    assert onboard_status(home / "supervisor")["steps"]["template"]["template"] == "operator"


def test_yes_with_no_model_flags_takes_the_local_ollama_or_names_the_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--yes` takes every default: the first local Ollama model, and the
    least-privilege template (`assistant`, plan decision #4) with the role
    following it. No local Ollama: a refusal that names the flags."""
    from skep.supervisor import onboard

    argv = ["--home", str(tmp_path / "home"), "onboard", "--yes", "--no-chat"]
    argv += ["--workdir", str(tmp_path / "work"), "--sandbox", "none"]
    nothing: tuple[list[str] | None, ...] = (None, [])
    for found in nothing:
        monkeypatch.setattr(onboard, "detect_ollama", lambda *a, found=found, **k: found)
        assert main(argv) == 1
        err = capsys.readouterr().err
        assert "no local Ollama with a model — pass --provider and --model" in err
        assert "finished steps are kept" in err

    fake = FakeOllama(models=["llama3.2"]).start()
    try:
        fake.script_reply("pong")
        monkeypatch.setattr(onboard, "detect_ollama", lambda *a, **k: ["llama3.2", "qwen3"])
        monkeypatch.setattr(onboard, "OLLAMA_LOCAL", fake.base_url)
        assert main(argv) == 0
    finally:
        fake.stop()
    steps = onboard_status(tmp_path / "home" / "supervisor")["steps"]
    assert (steps["model"]["provider"], steps["model"]["model"]) == ("ollama", "llama3.2")
    assert steps["template"]["template"] == "assistant"
    assert steps["role"]["role"] == "assistant"


def test_an_existing_non_git_workdir_is_registered_but_never_committed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    work = tmp_path / "notes"
    work.mkdir()
    (work / "today.md").write_text("mine\n")
    assert main(_onboard_flags(tmp_path / "home", work)) == 0
    out = capsys.readouterr().out
    assert f"using {work.resolve()}" in out
    assert "not a git repository — dispatch needs one" in out
    assert not (work / ".git").exists()
    record = onboard_status(tmp_path / "home" / "supervisor")["steps"]["workdir"]
    assert (record["created"], record["git"]) == (False, False)


def test_rerunning_the_role_step_keeps_the_first_run_counters(tmp_path: Path) -> None:
    """O4 counts turns and verdicts in first_run.json; changing the role must
    not reset them (or un-dismiss the transparency)."""
    home = tmp_path / "home"
    with _holder(home) as (holder, _store):
        first = onboard_role(holder, role="builder")
        path = home / "first_run.json"
        path.write_text(
            json.dumps({**first, "turns_seen": 7, "verdicts_seen": 19, "dismissed": True})
        )
        again = onboard_role(holder, role="operator")
        assert again == {**first, "turns_seen": 7, "verdicts_seen": 19, "dismissed": True,
                         "role": "operator"}  # fmt: skip
        with pytest.raises(ValueError, match="role must be one of builder, operator, assistant"):
            onboard_role(holder, role="admin")
    assert json.loads(path.read_text())["role"] == "operator"


def test_the_model_step_refuses_what_it_cannot_reach(tmp_path: Path) -> None:
    with _holder(tmp_path / "home") as (holder, store):
        with pytest.raises(ValueError, match="openai-compatible provider needs --endpoint"):
            onboard_model(holder, store, provider="openai-compatible", model="m")
        with pytest.raises(ValueError, match="unknown provider 'warp'"):
            onboard_model(holder, store, provider="warp", model="m")
    assert onboard_status(tmp_path / "home" / "supervisor")["next"] == "model"


def test_detect_ollama_lists_models_or_answers_none() -> None:
    from skep.supervisor.onboard import detect_ollama

    fake = FakeOllama(models=["llama3.2", "qwen3"]).start()
    url = fake.base_url
    try:
        assert detect_ollama(url) == ["llama3.2", "qwen3"]
    finally:
        fake.stop()
    assert detect_ollama(url, timeout=0.5) is None  # nothing answers there now


def test_rest_refuses_a_stale_preview_and_a_bad_role(config: SupervisorConfig) -> None:
    """The web form posts the sha256 of the table the operator saw; a
    different candidate is refused (they approve what they read), and no
    baseline is written."""
    client = serve_client(config)
    stale = client.post(
        "/api/onboard/template",
        json={"template": "builder", "expect_sha256": "0" * 64, "approved_by": "web"},
    )
    assert stale.status_code == 400 and "changed since it was shown" in stale.json()["detail"]
    assert not baseline_path(config.home).exists()
    bad = client.post("/api/onboard/role", json={"role": "admin"})
    assert bad.status_code == 400 and "role must be one of" in bad.json()["detail"]
    unknown = client.get("/api/onboard/template", params={"template": "warp"})
    assert unknown.status_code == 400 and "no policy template" in unknown.json()["detail"]


def test_check_tells_the_egress_truth_and_each_blocked_reason(tmp_path: Path) -> None:
    """ADR 0051 / I12: the model line says where prompts go (nowhere, this
    machine, or which cloud host), the workers line says what the baseline
    lets out; a schema-changed baseline and a vanished workdir each block."""
    from skep.supervisor.onboard import onboard_check
    from skep.supervisor.policy_baseline import write_lock
    from skep.supervisor.policy_templates import load_policy_template

    home = tmp_path / "home"
    supervisor = build_config(home, None).home

    def check() -> dict[str, Any]:
        return onboard_check(supervisor)

    def set_base_url(url: str) -> None:
        with _holder(home) as (_, store):
            store.set_setting(LLM_BASE_URL, url)

    assert check()["egress"] == [
        "model: no provider configured — nothing is sent",
        "workers: no host allowed by default; every connection asks",
        "telemetry: none",
    ]
    set_base_url("http://127.0.0.1:11434")
    assert check()["egress"][0] == "model: LOCAL (127.0.0.1) — nothing leaves this machine"
    set_base_url("https://api.example.com/v1")
    assert check()["egress"][0] == (
        "model: CLOUD — every assistant/worker prompt leaves this machine for api.example.com"
    )
    builder = load_policy_template("builder")
    write_lock(supervisor, builder, approved_by="t", schema_version=0)
    result = check()
    assert result["egress"][1] == (
        f"workers: {', '.join(builder.settings['default_network'])} allowed by the baseline; "
        "every other host asks"
    )
    assert result["checks"]["baseline"] == {
        "status": "blocked",
        "detail": "schema changed — `skep policy approve --reapprove`",
    }
    work = tmp_path / "work"
    assert main(_onboard_flags(home, work)) == 0
    work.rename(tmp_path / "moved")
    result = check()
    assert result["checks"]["workdir"] == {
        "status": "blocked",
        "detail": f"{work.resolve()} is NOT writable",
    }
    assert not result["ready"]


def test_the_last_step_attaches_to_a_running_daemon_and_opens_chat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """⑤'s hand-off (D7): a daemon that answers /api/status with this home's
    token is attached to — no second daemon — and `skep chat` opens with the
    role noted."""
    import subprocess

    import httpx

    import skep.cli_chat
    from skep.supervisor.serve.auth import TOKEN_FILE

    home = tmp_path / "home"
    supervisor = build_config(home, None).home
    supervisor.mkdir(parents=True)
    (supervisor / TOKEN_FILE).write_text("tok\n")
    monkeypatch.delenv("SKEP_SERVE_URL", raising=False)
    seen: list[dict[str, Any]] = []

    def get(url: str, headers: dict[str, str], timeout: float) -> httpx.Response:
        seen.append({"url": url, **headers})
        return httpx.Response(200)

    monkeypatch.setattr(httpx, "get", get)
    real_popen = subprocess.Popen  # git still runs (the workdir baseline)

    def popen(argv: list[str], *args: Any, **kwargs: Any) -> Any:
        assert "serve" not in argv, "started a second daemon"
        return real_popen(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", popen)
    chats: list[dict[str, Any]] = []

    def run_chat(**kwargs: Any) -> int:
        chats.append(kwargs)
        return 0

    monkeypatch.setattr(skep.cli_chat, "run_chat", run_chat)
    argv = [a for a in _onboard_flags(home, tmp_path / "work") if a != "--no-chat"]
    assert main(argv) == 0
    assert seen[0]["url"] == f"{skep.cli_chat.DEFAULT_URL}/api/status"
    assert seen[0][skep.cli_chat.TOKEN_HEADER] == "tok"
    assert chats == [{"home": supervisor, "url": skep.cli_chat.DEFAULT_URL}]
    assert "role: builder — noted in first_run.json" in capsys.readouterr().out


def test_with_no_daemon_the_last_step_starts_one_or_says_how(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import itertools
    import subprocess
    import sys
    import time

    import httpx

    import skep.cli_chat
    from skep.cli import _onboard_chat
    from skep.supervisor.serve.auth import TOKEN_FILE

    supervisor = build_config(tmp_path / "home", None).home
    supervisor.mkdir(parents=True)
    monkeypatch.setenv("SKEP_SERVE_URL", "http://127.0.0.1:8899")
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(200))
    monkeypatch.setattr(skep.cli_chat, "run_chat", lambda **kw: 0)
    started: list[list[str]] = []

    def popen(argv: list[str], **kwargs: Any) -> None:
        started.append(argv)
        assert kwargs["start_new_session"] is True  # outlives `skep onboard`
        (supervisor / TOKEN_FILE).write_text("tok")  # the daemon came up

    monkeypatch.setattr(subprocess, "Popen", popen)
    assert _onboard_chat(supervisor, "operator") == 0
    assert started == [
        [sys.executable, "-m", "skep", "--home", str(supervisor.parent), "serve",
         "--port", "8899"],
    ]  # fmt: skip
    assert "starting the daemon: skep serve --port 8899" in capsys.readouterr().out

    # A daemon that never answers: give up at the deadline and say what to run.
    (supervisor / TOKEN_FILE).unlink()
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: None)
    clock = itertools.count(0, 10)
    monkeypatch.setattr(time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    assert _onboard_chat(supervisor, "operator") == 1
    assert "the daemon did not come up — run skep serve, then skep chat" in (
        capsys.readouterr().err
    )
