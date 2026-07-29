"""v40-F12 (v36-F8): skep setup --template — preview, apply, switch diffs.

Plus the v19 replay pin: the add-README task under personal-dev completes in
one run with zero mid-run approvals and one landing approval — templates may
never regress the v19-F1 batch-gate economy."""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from skep.cli import main
from skep.profile import run_personal_setup
from skep.supervisor import RunStore
from skep.supervisor.cli_cmds import build_config
from skep.supervisor.policy_schema import (
    POLICY_DOCUMENT_SETTINGS_KEY,
    document_from_settings,
)

from .conftest import git
from .conftest import serve_client as _client
from .conftest import wait_terminal as _wait_terminal
from .fake_openai import FakeOpenAI

_VERIFY = [sys.executable, "-c", "import os; assert os.path.exists('README.md')"]


def _run_cli(home: Path, *argv: str) -> int:
    return main(["--home", str(home), *argv])


def _stored_document(home: Path) -> Any:
    store = RunStore(build_config(home, None).db_path)
    try:
        return document_from_settings(store.get_setting(POLICY_DOCUMENT_SETTINGS_KEY))
    finally:
        store.close()


def test_template_round_trip_writes_document_and_derived_knobs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    assert _run_cli(home, "setup", "--template", "personal-dev") == 0
    out = capsys.readouterr().out
    assert "policy template: personal-dev" in out
    assert "(pack: trusted_local_dev)" in out
    assert "net-pypi" in out  # the preview table IS the feature
    assert "applied template 'personal-dev'" in out
    assert "next steps:" in out

    document = _stored_document(home)
    assert document is not None and document.template == "personal-dev"
    store = RunStore(build_config(home, None).db_path)
    try:
        assert store.get_setting("default_network") == [
            "pypi.org",
            "files.pythonhosted.org",
            "registry.npmjs.org",
            "proxy.golang.org",
        ]
        assert store.get_setting("allowed_shell_commands") == []
    finally:
        store.close()


def test_dry_run_prints_the_table_and_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    assert _run_cli(home, "setup", "--template", "locked-down", "--dry-run") == 0
    out = capsys.readouterr().out
    assert "dry run — nothing written" in out
    assert "shell-gated" in out
    assert _stored_document(home) is None


def test_switching_templates_diffs_first_and_requires_apply(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    assert _run_cli(home, "setup", "--template", "personal-dev") == 0
    capsys.readouterr()

    refused = _run_cli(home, "setup", "--template", "locked-down")
    captured = capsys.readouterr()
    assert refused == 1
    assert "switching personal-dev -> locked-down would change:" in captured.out
    assert "- coding: allow edit workspace (coding-workspace)" in captured.out
    assert "re-run with --apply" in captured.err
    assert _stored_document(home).template == "personal-dev"  # unchanged

    assert _run_cli(home, "setup", "--template", "locked-down", "--apply") == 0
    assert _stored_document(home).template == "locked-down"


def test_unknown_template_teaches_the_known_names(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run_cli(tmp_path / "home", "setup", "--template", "warp") == 2
    err = capsys.readouterr().err
    assert "no policy template 'warp'" in err
    assert "personal-dev" in err


def _plan(steps: Sequence[dict[str, Any]], summary: str) -> str:
    import json

    return json.dumps(
        {
            "summary": summary,
            "required_tools": ["filesystem.write", "shell.run"],
            "steps": list(steps),
            "verify": {},
        }
    )


def test_v19_replay_under_personal_dev_one_run_one_approval(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The DoD pin: templates never regress the one-run-one-approval economy."""
    home = tmp_path / "home"
    assert _run_cli(home, "setup", "--template", "personal-dev") == 0
    config = build_config(home, None)
    client = _client(config)
    server = FakeOpenAI(api_key="sk-fake").start()
    monkeypatch.setenv("SKEP_TEST_LLM_KEY", "sk-fake")
    write_readme: dict[str, Any] = {
        "tool": "filesystem.write",
        "args": {"path": "README.md", "content": "# add-readme\n", "overwrite": True},
    }
    try:
        run_personal_setup(
            config.home.parent,
            provider="openai-compat",
            model="gpt-oss",
            endpoint=server.base_url,
            api_key_env="SKEP_TEST_LLM_KEY",
        )
        # Machine-specific knobs stay the operator's — the template never
        # widens trusted roots or picks the execution mode.
        client.put(
            "/api/policy",
            json={
                "trusted_workspace_roots": [str(tmp_path)],
                "default_execution_mode": "workspace",
            },
        )
        verify_step = {"tool": "shell.run", "args": {"argv": _VERIFY, "purpose": "verify"}}
        server.script_reply(
            _plan([write_readme, verify_step], "add a README (skep lands it)")
        )
        task_id = client.post(
            "/api/runs",
            json={
                "repo": str(repo),
                "instructions": "Create branch add-readme and write a README.",
                "network": ["*"],
                "env_allowlist": ["SKEP_TEST_LLM_KEY"],
            },
        ).json()["task_id"]
        run = _wait_terminal(client, task_id)
        assert run["state"] == "completed"

        # ≤ 2 runs (exactly one), ≤ 1 approval (exactly the landing one).
        store = RunStore(config.db_path)
        try:
            assert {r.task_id for r in store.recent_runs(10)} == {task_id}
        finally:
            store.close()
        assert client.get("/api/approvals").json()["approvals"] == []
        review_id = client.post(f"/api/runs/{task_id}/approvals").json()["review_id"]
        branch = client.post(
            f"/api/approvals/{review_id}/approve", json={"actor": "tester"}
        ).json()["branch"]
        assert branch == f"skep/{task_id}"
    finally:
        server.stop()
    assert "add-readme" in git(repo, "show", f"skep/{task_id}:README.md").stdout
