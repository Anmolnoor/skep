from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path
from typing import Any, cast

import pytest

from skep.cli import main
from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor.cli_cmds import build_config
from skep.supervisor.scheduler import make_schedule, make_template_schedule, run_due
from skep.supervisor.serve.jobs import Dispatcher
from skep.supervisor.serve.settings import ConfigHolder
from skep.supervisor.serve.tools import execute_mutation
from skep.supervisor.templates import TemplateParam, WorkflowTemplate

from .conftest import FAKE_WORKER, wait_terminal
from .conftest import serve_client as _client


def _worker_cmd() -> str:
    return shlex.join([sys.executable, str(FAKE_WORKER)])


def _seed_trusted_project(store: RunStore, repo: Path) -> None:
    store.add_project_policy(
        project_id="trusted-parity",
        name="Trusted Parity",
        strategy="trusted_local_dev",
        phase="build",
        policy={
            "default_execution_mode": "workspace",
            "auto_dispatch_allowed": True,
        },
    )
    store.add_project_binding(
        project_id="trusted-parity",
        binding_kind="repo_path",
        binding_value=str(repo),
    )


def _expected_decision() -> dict[str, object]:
    return {
        "verdict": "allow",
        "reason": "dispatch.auto_allowed.project_policy_match",
        "detail": None,
        "decided_by": None,  # v40-F8 additive field
        "project_id": "trusted-parity",
        "strategy": "trusted_local_dev",
        "phase": "build",
        "policy_source": "project_policy",
        # v23-F5: trusted dev workspace runs with no explicit network resolve
        # the package-registry hosts — identically on every entrypoint.
        "constraints": {
            "network_requested": None,
            "network_resolved": [
                "files.pythonhosted.org",
                "proxy.golang.org",
                "pypi.org",
                "registry.npmjs.org",
            ],
        },
    }


def _task_decision(config: SupervisorConfig, task_id: str) -> dict[str, object]:
    task_json = cast(
        dict[str, Any], json.loads((config.audit_dir / task_id / "task.json").read_text())
    )
    return cast(dict[str, object], task_json["dispatch_decision"])


def test_trusted_project_dispatch_decision_matches_across_entrypoints(
    repo: Path, config: SupervisorConfig, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = RunStore(config.db_path)
    try:
        _seed_trusted_project(store, repo)
        store.add_template(
            WorkflowTemplate(
                name="parity-template",
                instructions="Fix the bug. MODE:happy",
                params=(TemplateParam(name="unused", default="ok"),),
            )
        )
    finally:
        store.close()

    expected = _expected_decision()
    decisions: list[dict[str, object]] = []

    client = _client(config)
    http_task_id = str(
        client.post(
            "/api/runs",
            json={"repo": str(repo), "instructions": "Fix the bug. MODE:happy"},
        ).json()["task_id"]
    )
    wait_terminal(client, http_task_id)
    decisions.append(_task_decision(config, http_task_id))

    store = RunStore(config.db_path)
    holder = ConfigHolder(config, store)
    runner = Dispatcher(holder, store)
    try:
        chat_result = execute_mutation(
            "dispatch_run",
            {"repo": str(repo), "instructions": "Fix the bug. MODE:happy"},
            store=store,
            holder=holder,
            runner=runner,
            actor="chat-user",
        )
        chat_task_id = str(chat_result["task_id"])
        wait_terminal(client, chat_task_id)
        decisions.append(_task_decision(config, chat_task_id))

        store.add_schedule(
            make_schedule(
                name="parity-schedule",
                repo=repo,
                instructions="Fix the bug. MODE:happy",
                interval_seconds=86400,
                start_at="2026-06-11T00:00:00Z",
            )
        )
        schedule_results = run_due(store=store, config=config, now="2026-06-11T09:00:00Z")
        decisions.append(_task_decision(config, str(schedule_results[0].task_id)))

        template = store.get_template("parity-template")
        assert template is not None
        store.add_schedule(
            make_template_schedule(
                name="parity-template-schedule",
                template=template,
                params={},
                repo=repo,
                interval_seconds=86400,
                start_at="2026-06-11T10:00:00Z",
            )
        )
        template_results = run_due(store=store, config=config, now="2026-06-11T10:00:00Z")
        decisions.append(_task_decision(config, str(template_results[0].task_id)))
    finally:
        runner.shutdown()
        store.close()

    cli_home = tmp_path / "cli-home"
    cli_config = build_config(cli_home, _worker_cmd())
    cli_store = RunStore(cli_config.db_path)
    try:
        _seed_trusted_project(cli_store, repo)
    finally:
        cli_store.close()
    cli_code = main(
        [
            "--home",
            str(cli_home),
            "run",
            str(repo),
            "Fix the bug. MODE:happy",
            "--worker-cmd",
            _worker_cmd(),
            "--quiet",
        ]
    )
    captured = capsys.readouterr()
    assert cli_code == 0, captured.out + captured.err
    cli_store = RunStore(cli_config.db_path)
    try:
        latest_cli_task_id = cli_store.recent_runs(1)[0].task_id
    finally:
        cli_store.close()
    decisions.append(_task_decision(cli_config, latest_cli_task_id))

    assert decisions == [expected] * len(decisions)
