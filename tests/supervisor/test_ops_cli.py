"""v15 Step 6: the node + ops CLI."""

from __future__ import annotations

from pathlib import Path

import pytest

from skep.cli import main


def _run(home: Path, *args: str) -> int:
    return main(["--home", str(home), *args])


def test_node_add_and_list(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    home = tmp_path / "home"
    rc = _run(
        home, "node", "add", "localhost", "--trust", "trusted_local", "--cap", "ops.inspect.disk"
    )
    assert rc == 0
    assert "added node 'localhost'" in capsys.readouterr().out

    assert _run(home, "node", "list") == 0
    out = capsys.readouterr().out
    assert "localhost" in out and "ops.inspect.disk" in out


def test_node_add_rejects_bad_capability(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    rc = _run(home, "node", "add", "n1", "--cap", "ops.launch.missiles")
    assert rc != 0


def test_ops_run_resolves_decision(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    home = tmp_path / "home"
    _run(home, "node", "add", "localhost", "--cap", "ops.inspect.disk")
    capsys.readouterr()
    # A read-only disk check on a trusted local node is allowed unattended.
    assert _run(home, "ops", "run", "disk-usage", "--node", "localhost") == 0
    assert "disk-usage: allow" in capsys.readouterr().out

    # A check whose capability the node does not grant resolves to a denial.
    assert _run(home, "ops", "run", "service-health", "--node", "localhost") != 0
    assert "deny" in capsys.readouterr().out


def test_ops_run_gated_real_execution(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """v32: bare `ops run` on a mutating capability shows a dry-run plan and
    touches nothing; --approve runs the bounded real pass."""
    home = tmp_path / "home"
    root = tmp_path / "cache"
    root.mkdir()
    target = root / "old.log"
    target.write_text("stale data")
    _run(
        home,
        "node",
        "add",
        "localhost",
        "--cap",
        "ops.maintenance.clean_paths",
    )
    capsys.readouterr()

    # Bare run: a dry-run plan, nothing deleted.
    rc = _run(
        home,
        "ops",
        "run",
        "ops.maintenance.clean_paths",
        "--node",
        "localhost",
        "--arg",
        f"paths={target}",
        "--arg",
        f"allowed_roots={root}",
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "dry-run" in out
    assert "--approve" in out
    assert target.exists()  # planning touched nothing

    # --approve: the bounded real pass deletes the file and prints evidence.
    rc = _run(
        home,
        "ops",
        "run",
        "ops.maintenance.clean_paths",
        "--node",
        "localhost",
        "--approve",
        "--arg",
        f"paths={target}",
        "--arg",
        f"allowed_roots={root}",
    )
    assert rc == 0
    assert "executed" in capsys.readouterr().out
    assert not target.exists()


def test_ops_run_approve_refuses_out_of_bounds_paths(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    root = tmp_path / "cache"
    root.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("keep me")
    _run(home, "node", "add", "localhost", "--cap", "ops.maintenance.clean_paths")
    capsys.readouterr()

    rc = _run(
        home,
        "ops",
        "run",
        "ops.maintenance.clean_paths",
        "--node",
        "localhost",
        "--approve",
        "--arg",
        f"paths={secret}",
        "--arg",
        f"allowed_roots={root}",
    )
    # The decision denies (path outside bounded roots); nothing executes.
    assert rc != 0
    assert secret.exists()


def test_ops_plan_and_run_over_http(tmp_path: Path) -> None:
    """v32-F3: /plan previews (mutates nothing); /run is the gated real pass."""
    from skep.supervisor import SupervisorConfig

    from .conftest import serve_client

    home = tmp_path / "home"
    _run(home, "node", "add", "localhost", "--cap", "ops.maintenance.clean_paths")
    root = tmp_path / "cache"
    root.mkdir()
    target = root / "old.log"
    target.write_text("stale")

    config = SupervisorConfig(home=home / "supervisor", worker_command=("false",))
    client = serve_client(config)
    body = {
        "node_id": "localhost",
        "capability": "ops.maintenance.clean_paths",
        "arguments": {"paths": [str(target)], "allowed_roots": [str(root)]},
    }

    planned = client.post("/api/ops/plan", json=body).json()
    assert planned["decision"]["dry_run"] is True
    assert planned["plan"]["targets"] == [str(target)]
    assert target.exists()  # planning touched nothing

    ran = client.post("/api/ops/run", json=body).json()
    assert ran["executed"] is True
    assert ran["evidence"]["bytes_freed"] > 0
    assert not target.exists()

    # An out-of-bounds path is refused by the executor (the last guard).
    escapee = tmp_path / "secret.txt"
    escapee.write_text("keep")
    bad = client.post(
        "/api/ops/run",
        json={
            "node_id": "localhost",
            "capability": "ops.maintenance.clean_paths",
            "arguments": {"paths": [str(escapee)], "allowed_roots": [str(root)]},
        },
    )
    assert bad.status_code == 409
    assert escapee.exists()


def test_docs_describe_gated_ops_execution() -> None:
    how = (Path(__file__).resolve().parents[2] / "docs" / "how-it-works.md").read_text()
    assert "## Local Ops" in how
    assert "--approve" in how
    assert "last guard" in how.lower()


def test_ops_schedule_add_only_accepts_conservative_checks(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    _run(home, "node", "add", "localhost", "--cap", "ops.inspect.disk")
    capsys.readouterr()
    assert (
        _run(home, "ops", "schedule", "add", "disk-usage", "--node", "localhost", "--every", "1d")
        == 0
    )
    assert "scheduled ops check" in capsys.readouterr().out
    # An unknown node is refused.
    assert _run(home, "ops", "schedule", "add", "disk-usage", "--node", "ghost") != 0
