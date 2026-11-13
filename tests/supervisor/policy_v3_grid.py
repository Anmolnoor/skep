"""ES-F (ADR 0059 P3-1): the probe grid the policy v3 tests share.

Five home states x four roles x two chains x two repos x the f1-01 probe
classes, decided through ``policy_decide`` — the REST/CLI/hook/ACP verb.
``tests/fixtures/policy_v3/pre_v3_verdicts.json`` is this grid answered by
the code BEFORE policy v3 (frozen from 3e2b6b5); the property test holds
the new answers to it. Regenerate only with the pre-v3 code:
``python -m tests.supervisor.policy_v3_grid > tests/fixtures/policy_v3/pre_v3_verdicts.json``.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from skep.supervisor import RunStore
from skep.supervisor import policy_baseline as pb
from skep.supervisor.cli_cmds import build_config
from skep.supervisor.policy_schema import POLICY_DOCUMENT_SETTINGS_KEY
from skep.supervisor.policy_templates import load_policy_template
from skep.supervisor.serve.actions import policy_decide
from skep.supervisor.serve.settings import ConfigHolder

from .conftest import predating_home

HOMES = ("legacy", "zero", "builder", "assistant", "operator", "tampered")
ROLES: tuple[str | None, ...] = (None, "builder", "operator", "assistant")
CHAINS: tuple[tuple[str, ...], ...] = ((), ("queen@assistant",))

# (id, scope, action, value) — ``{home}`` is the fake user home, ``{skep}``
# the skep home, ``{repo}`` the bound checkout. f1-01's 19 classes plus the
# unmatched shapes B77 is about.
PROBES: tuple[tuple[str, str, str, str], ...] = (
    ("floor-ssh", "filesystem", "read", "{home}/.ssh/id_rsa"),
    ("floor-aws", "filesystem", "read", "{home}/.aws/credentials"),
    ("floor-token", "filesystem", "read", "{skep}/serve-token"),
    ("floor-gh", "filesystem", "read", "{home}/.config/gh/hosts.yml"),
    ("repo-file", "filesystem", "read", "{repo}/README.md"),
    ("etc-hostname", "filesystem", "read", "/etc/hostname"),
    ("write-workspace", "filesystem", "write", "workspace"),
    ("write-etc", "filesystem", "write", "/etc/x"),
    ("sh-uv-sync", "shell", "run", "uv sync"),
    ("sh-npm-install", "shell", "run", "npm install left-pad"),
    ("sh-git-status", "shell", "run", "git status"),
    ("sh-df", "shell", "run", "df -h"),
    ("sh-rm", "shell", "run", "rm -rf build"),
    ("sh-chain", "shell", "run", "git status && rm -rf build"),
    ("sh-opaque", "shell", "run", "echo $(cat x)"),
    ("sh-cat-key", "shell", "run", "cat {home}/.ssh/id_rsa"),
    ("sh-push", "shell", "run", "git push origin main"),
    ("sh-sudo", "shell", "run", "sudo ls"),
    ("sh-bg", "shell", "run_background", "npm run dev"),
    ("sh-repo", "shell", "run_repo", "make"),
    ("fetch-github", "network", "fetch", "github.com"),
    ("fetch-example", "network", "fetch", "example.com"),
    ("fetch-evil", "network", "fetch", "evil.example"),
    ("connect-pypi", "network", "connect", "pypi.org"),
    ("connect-odd", "network", "connect", "odd.example"),
    ("search", "network", "search", "ddgs"),
    ("mcp-transfer", "mcp", "call", "bank:budget_transfer"),
    ("coding-edit", "coding", "edit", "workspace"),
    ("email-send", "email", "send", "mail:send"),
    ("browse-act", "browse", "act", "browser:click"),
)


def build_home(root: Path, kind: str) -> tuple[Path, Path]:
    """A home of ``kind`` with one bound trusted_local_dev repo whose run
    policy allows ``git status`` and ``uv sync``; the global document asks
    for one host and denies another (the ACP demo shape)."""
    repo, home = root / "repo", root / "home"
    repo.mkdir(parents=True)
    (repo / "README.md").write_text("hi\n")
    db_path = build_config(home, None).db_path
    store = RunStore(predating_home(db_path) if kind == "legacy" else db_path)
    try:
        store.set_setting("default_execution_mode", "workspace")
        store.set_setting("allowed_shell_commands", [["git", "status"], ["uv", "sync"]])
        network = {
            "scope": "network",
            "require_approval": [
                {"rule_id": "net:ask.example", "action": "fetch", "pattern": "example.com"}
            ],
            "deny": [{"rule_id": "net:evil.example", "action": "fetch", "pattern": "evil.example"}],
        }
        store.set_setting(POLICY_DOCUMENT_SETTINGS_KEY, json.dumps({"scopes": [network]}))
        store.add_project_policy(
            project_id="app", name="app", strategy="trusted_local_dev", phase="build", policy={}
        )
        store.add_project_binding(
            project_id="app", binding_kind="repo_path", binding_value=str(repo.resolve())
        )
    finally:
        store.close()
    config_home = build_config(home, None).home
    if kind in ("builder", "assistant", "operator", "tampered"):
        template = "builder" if kind == "tampered" else kind
        pb.write_lock(config_home, load_policy_template(template), approved_by="op")
    if kind == "tampered":
        (config_home / "policy" / "baseline.lock").unlink()  # missing lock = tampered
    return home, repo


Decider = Callable[..., dict[str, Any]]


def rows(root: Path, kind: str, decider: Decider | None = None) -> Iterator[dict[str, Any]]:
    """Every grid cell of one home as ``{key, verdict, rule_id, decided_by}``.
    ``decider(holder, store, **kwargs)`` defaults to ``policy_decide``."""
    decide = decider or policy_decide
    home, repo = build_home(root, kind)
    config = build_config(home, None)
    fake_home = os.environ["HOME"]
    for probe_id, scope, action, template in PROBES:
        value = template.format(home=fake_home, skep=config.home, repo=repo)
        for where in ("none", "repo"):
            for role in ROLES:
                for chain in CHAINS:
                    store = RunStore(config.db_path)
                    try:
                        record = decide(
                            ConfigHolder(config, store), store, scope=scope, action=action,
                            value=value, repo=str(repo) if where == "repo" else None,
                            role=role, delegation_chain=chain,
                        )  # fmt: skip
                    finally:
                        store.close()
                    key = "|".join((kind, probe_id, where, role or "-", ",".join(chain) or "-"))
                    yield {
                        "key": key,
                        "verdict": record["verdict"],
                        "rule_id": record["rule_id"],
                        "decided_by": record["decided_by"],
                    }


def _freeze() -> None:  # pragma: no cover - run by hand against the pre-v3 code
    """One ``"key": "verdict rule_id decided_by"`` line per cell."""
    out: dict[str, str] = {}
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["HOME"] = str(Path(tmp) / "fakehome")
        os.environ["SKEP_HOME"] = str(Path(tmp) / "skep-home")
        for kind in HOMES:
            for row in rows(Path(tmp) / kind, kind):
                out[row["key"]] = f"{row['verdict']} {row['rule_id']} {row['decided_by']}"
    lines = (f"{json.dumps(key)}: {json.dumps(out[key])}" for key in sorted(out))
    sys.stdout.write("{\n" + ",\n".join(lines) + "\n}\n")


if __name__ == "__main__":  # pragma: no cover
    _freeze()
