"""Phase 05 5.2: `skep doctor` grouped into Health, Security posture and
Projects needing upgrade — each upgrade line one command that works (I9) —
and `--offline`, which makes no network call (I8: it says it skipped)."""

from __future__ import annotations

import json
import shlex
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from skep import status
from skep.cli import main
from skep.supervisor import sandbox
from skep.supervisor.store import RunStore


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path / "user"))
    monkeypatch.setattr(
        status,
        "sandbox_availability",
        lambda: sandbox.SandboxAvailability(True, backend="bubblewrap"),
    )
    path = tmp_path / "home"
    (path / "supervisor").mkdir(parents=True)
    return path


def _project(home: Path, project_id: str, policy: dict[str, object], repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    store = RunStore(home / "supervisor" / "supervisor.sqlite3")
    try:
        store.add_project_policy(
            project_id=project_id, name=project_id, strategy="trusted_local_dev",
            phase="build", policy=policy,
        )  # fmt: skip
        store.add_project_binding(
            project_id=project_id, binding_kind="repo_path", binding_value=str(repo)
        )
    finally:
        store.close()


SANDBOXED = {"default_execution_mode": "sandbox", "strict_read_confinement": True}


def _upgrades(home: Path) -> dict[str, dict[str, object]]:
    return {u["project_id"]: u for u in status.build_status(home, offline=True)["project_upgrades"]}


def test_each_upgrade_prints_the_command_that_fixes_it(home: Path, tmp_path: Path) -> None:
    python = tmp_path / "py repo"  # a space: the command must quote it
    (python / "tests").mkdir(parents=True)
    (python / "pyproject.toml").write_text("[project]\nname='r'\n")
    _project(home, "loose", {"default_execution_mode": "workspace", "verify_command": "true"},
             tmp_path / "loose")  # fmt: skip
    _project(home, "nopin", {**SANDBOXED, "verify_command": ""}, python)
    _project(home, "bare", {**SANDBOXED}, tmp_path / "bare")
    _project(home, "open", {"default_execution_mode": "sandbox", "verify_command": "true"},
             tmp_path / "open")  # fmt: skip
    _project(home, "fine", {**SANDBOXED, "verify_command": "true"}, tmp_path / "fine")

    ups = _upgrades(home)
    assert sorted(ups) == ["bare", "loose", "nopin", "open"]
    setup = "skep project setup {} --project-id {}"
    assert ups["loose"]["command"] == (
        setup.format(tmp_path / "loose", "loose") + " --execution-mode sandbox --strict-reads"
    )
    assert ups["nopin"]["command"] == (
        setup.format(shlex.quote(str(python)), "nopin") + " --verify-command pytest"
    )
    # no test entry point: the one input the operator must supply, said so
    assert ups["bare"]["command"] == (
        setup.format(tmp_path / "bare", "bare") + " --verify-command '<your test command>'"
    )
    assert ups["open"]["command"] == setup.format(tmp_path / "open", "open") + " --strict-reads"
    assert ups["loose"]["needs"] == ["sandbox", "strict_reads"]


def test_strict_reads_is_not_asked_for_where_the_backend_cannot_confine(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        status, "sandbox_availability", lambda: sandbox.SandboxAvailability(True, backend="docker")
    )
    _project(home, "open", {"default_execution_mode": "sandbox", "verify_command": "true"},
             tmp_path / "open")  # fmt: skip
    assert _upgrades(home) == {}


def test_report_is_grouped_in_text_and_json(
    home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _project(home, "loose", {"default_execution_mode": "workspace", "verify_command": "true"},
             tmp_path / "loose")  # fmt: skip
    main(["--home", str(home), "doctor", "--offline"])
    text = capsys.readouterr().out
    health, rest = text.split("Security posture:")
    posture, upgrades = rest.split("Projects needing upgrade:")
    assert "Health: fail" in health and "- profile: blocked" in health
    for line in ("- sandbox: ready", "- provider key relay:", "- baseline lock: none approved"):
        assert line in posture
    assert f"- loose: skep project setup {tmp_path / 'loose'} --project-id loose" in upgrades
    # the per-project facts moved into the section — not said twice
    assert "run workers unsandboxed" not in text

    main(["--home", str(home), "doctor", "--offline", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["groups"] == {
        "health": ["required", "workers", "coding_engines", "approvals", "memory"],
        "security_posture": ["sandbox", "provider_key_relay", "baseline_lock"],
        "projects_needing_upgrade": ["loose"],
    }
    assert payload["baseline_lock"]["state"] == "none"
    assert {"required", "provider", "queen", "advisories"} <= payload.keys()


def test_no_upgrades_says_none(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    main(["--home", str(home), "doctor", "--offline"])
    assert "Projects needing upgrade:\n- none" in capsys.readouterr().out


@pytest.fixture
def provider_hits() -> Iterator[tuple[str, list[str]]]:
    hits: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def _answer(self) -> None:
            hits.append(self.path)
            body = b'{"models": [{"name": "m"}], "message": {"content": "ok"}}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST = _answer

        def log_message(self, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}", hits
    server.shutdown()


def test_offline_makes_no_network_call(
    home: Path, provider_hits: tuple[str, list[str]], capsys: pytest.CaptureFixture[str]
) -> None:
    endpoint, hits = provider_hits
    setup = ["--home", str(home), "setup", "--personal", "--provider", "ollama"]
    assert main([*setup, "--model", "m", "--endpoint", endpoint]) == 0
    capsys.readouterr()
    hits.clear()  # setup prints a live doctor report of its own

    assert main(["--home", str(home), "doctor", "--offline"]) == 0
    out = capsys.readouterr().out
    assert hits == []
    assert "- provider: skipped - skipped (--offline)" in out
    assert "- coding_worker: skipped (--offline): m via" in out

    main(["--home", str(home), "doctor"])  # the default probes, live
    assert "/api/tags" in hits and len(hits) >= 2


def test_a_missing_bwrap_names_the_install_command(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = sandbox.SandboxAvailability(False, "missing_binary", "bwrap was not found on PATH")
    monkeypatch.setattr(status, "sandbox_availability", lambda: missing)
    monkeypatch.setattr(sys, "platform", "linux")
    check = status.build_status(home, offline=True)["sandbox"]
    assert check["status"] == "unavailable"
    assert "sudo apt install bubblewrap" in check["next_step"]
    assert "sudo dnf install bubblewrap" in check["next_step"]
    report = status.format_doctor_report({"overall": "blocked", "sandbox": check})
    assert "Next step: install bubblewrap" in report


def test_a_key_the_completion_refuses_fails_health(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Review R1 D1: ollama.com lists models to anyone and 401s the chat — the
    listing-only probe said `Health: pass` above `coding_worker: blocked`."""
    from tests.supervisor.fake_ollama import FakeOllama

    fake = FakeOllama(api_key="sk-good", public_listing=True).start()
    try:
        monkeypatch.setenv("SKEP_R1_KEY", "sk-dead")
        setup = ["--home", str(home), "setup", "--personal", "--provider", "ollama"]
        main([*setup, "--model", "llama3.2", "--endpoint", fake.base_url,
              "--api-key-env", "SKEP_R1_KEY"])  # fmt: skip
        capsys.readouterr()
        assert main(["--home", str(home), "doctor"]) == 1
        out = capsys.readouterr().out
        assert "Health: fail" in out and "Skep: blocked" in out
        assert "- provider: blocked" in out and "401" in out
        assert "sk-dead" not in out
        # --offline dials nothing, so it cannot say the key is dead
        assert main(["--home", str(home), "doctor", "--offline"]) == 0
        # a working key passes
        monkeypatch.setenv("SKEP_R1_KEY", "sk-good")
        assert main(["--home", str(home), "doctor"]) == 0
        assert "Health: pass" in capsys.readouterr().out
    finally:
        fake.stop()


def test_offline_never_exchanges_a_copilot_token(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Review R1 D2: the worker credential and the relay summary resolve a
    Copilot key by EXCHANGING it at api.github.com — not under --offline."""
    from skep.profile import run_personal_setup
    from skep.supervisor.serve import llm_copilot

    calls: list[str] = []

    def exchange(token: str, **_: object) -> tuple[str, float]:
        calls.append(token)
        return "bearer", 9e9

    monkeypatch.setattr(llm_copilot, "exchange_copilot_token", exchange)
    monkeypatch.setenv("GITHUB_TOKEN", "ghu_offline_token")
    run_personal_setup(
        home, provider="openai-compat", model="gpt-5-mini",
        endpoint="https://api.githubcopilot.com", api_key_env="GITHUB_TOKEN",
    )  # fmt: skip
    payload = status.build_status(home, offline=True)
    assert calls == []
    assert payload["workers"]["coding_worker"]["status"] == "skipped"
    assert payload["provider_key_relay"]["relayed"] is True


def test_a_slug_bound_project_names_its_managed_checkout(home: Path, tmp_path: Path) -> None:
    """Review R1 D4: a repo_slug binding printed a literal `<repo>`."""
    store = RunStore(home / "supervisor" / "supervisor.sqlite3")
    try:
        for project_id in ("cloned", "gone"):
            store.add_project_policy(
                project_id=project_id, name=project_id, strategy="trusted_local_dev",
                phase="build", policy={**SANDBOXED, "verify_command": ""},
            )  # fmt: skip
            store.add_project_binding(
                project_id=project_id, binding_kind="repo_slug", binding_value=project_id
            )
    finally:
        store.close()
    (home / "repos" / "cloned").mkdir(parents=True)
    ups = _upgrades(home)
    checkout = home / "repos" / "cloned"
    assert ups["cloned"]["command"] == (
        f"skep project setup {checkout} --project-id cloned --verify-command '<your test command>'"
    )
    assert ups["gone"]["command"] is None
    instruction = str(ups["gone"]["instruction"])
    assert "<repo>" not in instruction and "repos/gone" in instruction
    report = status.format_doctor_report(status.build_status(home, offline=True))
    assert "- gone: no checkout" in report


def test_a_key_the_provider_echoes_never_reaches_doctor(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Review R2 F1: the worker probe printed the provider's words, so an
    upstream quoting the key back ("Incorrect API key provided: ...") put it
    into `skep doctor` text and JSON."""
    from skep.profile import run_personal_setup
    from tests.supervisor.fake_openai import FakeOpenAI

    fake = FakeOpenAI(api_key="good1", models=["gpt"])
    fake.echo_key = fake.public_listing = True
    fake.start()
    monkeypatch.setenv("SKEP_R2_KEY", "sk-leak9")
    try:
        run_personal_setup(
            home, provider="openai-compat", model="gpt", endpoint=fake.base_url,
            api_key_env="SKEP_R2_KEY",
        )  # fmt: skip
        assert main(["--home", str(home), "doctor"]) == 1
        text = capsys.readouterr().out
        main(["--home", str(home), "doctor", "--json"])
        payload = capsys.readouterr().out
    finally:
        fake.stop()
    assert "401" in text and "401" in payload
    assert "sk-leak9" not in text + payload


def test_a_refused_copilot_exchange_is_blocked_health_not_a_crash(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review R2 F11: the registry-first worker read let the exchange's
    OllamaError escape — doctor crashed, the relay planner skipped its
    config-error handling. Now it is a blocked worker, classified by status."""
    from skep.supervisor.provider_relay import relay_plan
    from skep.supervisor.providers import ProviderProfile
    from skep.supervisor.serve import llm_copilot
    from skep.supervisor.serve.llm import OllamaError

    def refused(_token: str, **_: object) -> tuple[str, float]:
        raise OllamaError("copilot token exchange failed: 401 from api.github.com", status=401)

    monkeypatch.setattr(llm_copilot, "exchange_copilot_token", refused)
    store = RunStore(home / "supervisor" / "supervisor.sqlite3")
    try:
        store.upsert_provider_profile(
            ProviderProfile(
                "copilot", "openai_compat", "https://api.githubcopilot.com", "gpt-5-mini",
                active=True,
            )
        )  # fmt: skip
    finally:
        store.close()
    (home / "supervisor" / "llm-secret-copilot").write_text("ghu_r2dead\n")
    worker = status.build_status(home)["workers"]["coding_worker"]
    assert (worker["status"], worker["kind"], worker["http_status"]) == ("blocked", "auth", 401)
    plan, why = relay_plan(home)
    assert plan is None and "401" in why
