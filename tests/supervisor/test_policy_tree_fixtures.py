"""v124 review: the policy tree's tests/web fixtures are the engine's real
answers, never hand-made (I8). Each home below is built the way the browser
check builds one (`skep policy approve --template builder`, a bound project,
schedules), its routes are read through the serve app exactly as
policies.js reads them, and the JSON is compared with what
tests/web/fixtures/ commits. A drift fails here; regenerate with
``SKEP_REGEN_FIXTURES=1`` and re-run tests/web.
"""

from __future__ import annotations

import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest

from skep.cli import main
from skep.supervisor import RunStore
from skep.supervisor.cli_cmds import build_config
from skep.supervisor.policy_baseline import baseline_path
from skep.supervisor.policy_schema import SCOPES
from skep.supervisor.scheduler import make_schedule

from .conftest import git, serve_client

FIXTURES = Path(__file__).parents[1] / "web" / "fixtures"
ROOT = "/fixture"  # every throwaway path reads as this, so the JSON is stable
# Values that move every run (clock, hash, token): the shape stays, the value is pinned.
VOLATILE = {
    "approved_at": "2026-11-18T09:00:00Z",
    "created_at": "2026-11-18T09:00:00Z",
    "next_run_at": "2026-11-19T09:00:00Z",
    "tampered_since": "2026-11-18T10:00:00Z",
    "sha256": "0" * 64,
    "relocked_from": "0" * 64,
    "approved_by": "operator",  # the CLI stamps $USER
}


def _scrub(value: Any, roots: list[str]) -> Any:
    if isinstance(value, dict):
        return {
            k: VOLATILE[k] if k in VOLATILE and v is not None else _scrub(v, roots)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_scrub(v, roots) for v in value]
    if isinstance(value, str):
        for root in roots:
            value = value.replace(root, ROOT)
        value = value.replace(sys.executable, "python")  # worker_cmd names this venv
        value = re.sub(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", VOLATILE["approved_at"], value)
        return re.sub(r"sha256 [0-9a-f]{12}", "sha256 " + "0" * 12, value)
    return value


def _repo(path: Path) -> Path:
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    return path


def _home(tmp_path: Path, *, approve: bool) -> Any:
    config = build_config(tmp_path / "home", None)
    repo = _repo(tmp_path / "app")
    store = RunStore(config.db_path)
    try:
        store.set_setting("policy_engine_v2_scopes", list(SCOPES))
        store.set_setting("default_execution_mode", "workspace")
        store.set_setting("auto_approve", True)
        store.add_project_policy(
            project_id="app",
            name="app",
            strategy="trusted_local_dev",
            phase="build",
            policy={"default_max_actions": 7},
        )
        store.add_project_binding(
            project_id="app", binding_kind="repo_path", binding_value=str(repo)
        )
        store.add_project_policy(
            project_id="loose", name="loose", strategy="trusted_local_dev", phase="build", policy={}
        )
        schedules: list[dict[str, Any]] = [
            {"name": "nightly", "repo": repo, "role": "builder"},
            {"name": "extra", "repo": repo, "network": ["example.com"]},
            {"name": "stray", "repo": tmp_path / "stray"},
            {"name": "backup", "repo": repo, "worker_kind": "script", "instructions": "true"},
            {"name": "morning", "repo": repo, "worker_kind": "prompt"},
            {"name": "memo", "repo": repo, "worker_kind": "note"},
            {"name": "digest", "repo": repo, "worker_kind": "digest"},
        ]
        for row in schedules:
            store.add_schedule(
                make_schedule(
                    interval_seconds=86400,
                    start_at="2026-11-19T09:00:00Z",
                    **{"instructions": "fix the bug", **row},
                )
            )
    finally:
        store.close()
    if approve:
        assert main(["--home", str(tmp_path / "home"), "policy", "approve", "--template",
                     "builder", "--yes"]) == 0  # fmt: skip
    return config


def _why(repo: str, scope: str, action: str, value: str, extra: str = "") -> str:
    # The query string policies.js builds (encodeURIComponent of each part).
    return (
        f"/api/policy/why?repo={quote(repo, safe='')}&scope={scope}&action={action}"
        f"&value={quote(value, safe='')}{extra}"
    )


def _read(tmp_path: Path, config: Any, paths: list[str]) -> dict[str, Any]:
    roots = sorted({str(tmp_path), str(tmp_path.resolve())}, key=len, reverse=True)
    client = serve_client(config)
    out: dict[str, Any] = {}
    for path in paths:
        real = str(tmp_path.resolve())
        response = client.get(
            path.replace(quote(ROOT, safe=""), quote(real, safe="")).replace(ROOT, real)
        )
        assert response.status_code == 200, (path, response.text)
        out[f"GET {path}"] = _scrub(response.json(), roots)
    return out


def _check(name: str, payload: dict[str, Any]) -> None:
    target = FIXTURES / f"{name}.json"
    text = json.dumps(payload, indent=1, sort_keys=True) + "\n"
    if os.environ.get("SKEP_REGEN_FIXTURES"):
        target.parent.mkdir(exist_ok=True)
        target.write_text(text, encoding="utf-8")
    assert target.read_text(encoding="utf-8") == text, f"regenerate {target.name}"


REPO = f"{ROOT}/app"
RSOP = f"/api/repos/{quote(REPO, safe='')}/rsop"
CHAIN = "&role=assistant&delegation_chain=" + quote("schedule:nightly@builder", safe="")
TREE_READS = [
    "/api/policy/registry",
    "/api/schedules",
    "/api/projects",
    "/api/roles",
    "/api/policy/baseline",
    "/api/policy/explain?subject=baseline",
    RSOP,
]


def test_locked_builder_home_fixture(tmp_path: Path) -> None:
    config = _home(tmp_path, approve=True)
    _check("policy-locked", _read(tmp_path, config, [
        *TREE_READS,
        _why(REPO, "shell", "run", "uv sync"),
        _why(REPO, "shell", "run", "uv sync", CHAIN),
        _why(REPO, "network", "search", "python"),
    ]))  # fmt: skip


def test_no_baseline_home_fixture(tmp_path: Path) -> None:
    """A fresh home (the store seeded it), nothing approved: the zero state binds."""
    config = _home(tmp_path, approve=False)
    _check("policy-none", _read(tmp_path, config, [
        *TREE_READS, _why(REPO, "shell", "run", "uv sync"),
    ]))  # fmt: skip


def test_tampered_home_fixture(tmp_path: Path) -> None:
    """The builder lock, then baseline.json edited behind it."""
    config = _home(tmp_path, approve=True)
    path = baseline_path(config.home)
    path.chmod(path.stat().st_mode | stat.S_IWUSR)
    layer = json.loads(path.read_text(encoding="utf-8"))
    layer["settings"]["default_max_actions"] = 9999
    path.write_text(json.dumps(layer), encoding="utf-8")
    _check("policy-tampered", _read(tmp_path, config, [
        "/api/policy/baseline", "/api/policy/explain?subject=baseline", RSOP,
        _why(REPO, "shell", "run", "uv sync"),
    ]))  # fmt: skip


@pytest.mark.parametrize("name", ["policy-locked", "policy-none", "policy-tampered"])
def test_fixture_is_committed(name: str) -> None:
    assert (FIXTURES / f"{name}.json").is_file()
