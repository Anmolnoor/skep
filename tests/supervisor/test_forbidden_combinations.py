"""ES-F6 (edge sweep §5.4, items 1 and 3): forbidden combinations, as a
property over every shipped template, role and engine.

1. No reachable verdict allows reading a secret-floor path.
3. No Queen host-lane rule lets a package install run without a card.

Each template reaches a home two ways at once — the global document
`skep setup --template` stores and the approved baseline — the widest
stack the shipped policy can build. Imports only pre-existing faces on
purpose, so the same file fails against the tree before ES-F1/ES-F3.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor import policy_baseline as pb
from skep.supervisor.policy_schema import POLICY_DOCUMENT_SETTINGS_KEY, SCOPES
from skep.supervisor.policy_templates import TEMPLATE_NAMES, load_policy_template
from skep.supervisor.roles import SHIPPED_ROLES
from skep.supervisor.serve.actions import policy_decide
from skep.supervisor.serve.fileio import queen_filesystem_decision
from skep.supervisor.serve.settings import ConfigHolder
from skep.supervisor.serve.tools import queen_shell_decision

from .conftest import predating_home

ROLES = (None, *SHIPPED_ROLES)
ENGINES = {"v1": [], "v2": list(SCOPES)}
HOME_SECRETS = (
    ".ssh/id_rsa",
    ".aws/credentials",
    ".gnupg/private-keys-v1.d/key",
    ".config/gh/hosts.yml",
    ".netrc",
    ".git-credentials",
    ".docker/config.json",
    ".kube/config",
)
SKEP_SECRETS = ("serve-token", "llm-secret", "llm-secret-openai", "telegram-secret", "mail-secret")
INSTALLS = (
    "npm install left-pad",
    "npm i left-pad",
    "pnpm install",
    "pnpm add x",
    "yarn add x",
    "pip install requests",
    "uv sync",
    "uv add requests",
    "uv pip install requests",
    "cargo install ripgrep",
    "go install example.com/x@latest",
    "go get example.com/x",
    "gem install rails",
    "brew install jq",
    "apt-get install jq",
)


@pytest.fixture(params=[(t, e) for t in TEMPLATE_NAMES for e in ENGINES], ids=lambda p: "-".join(p))
def stack(
    request: pytest.FixtureRequest,
    config: SupervisorConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[RunStore, ConfigHolder, list[str]]:
    template_name, engine = request.param
    home = tmp_path / "fakehome"
    secrets = [home / rel for rel in HOME_SECRETS]
    project = tmp_path / "project"  # a workon-bound path: an operator root
    secrets += [project / ".env", project / ".env.local"]
    secrets += [config.home / name for name in SKEP_SECRETS]
    secrets += [config.home.parent / "serve.env"]
    for path in secrets:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("CANARY\n")
    link = project / "notes.txt"
    link.symlink_to(home / ".ssh" / "id_rsa")
    monkeypatch.setenv("HOME", str(home))
    template = load_policy_template(template_name)
    store = RunStore(predating_home(config.db_path))
    store.set_setting("policy_engine_v2_scopes", ENGINES[engine])
    store.set_setting(POLICY_DOCUMENT_SETTINGS_KEY, template.document().model_dump_json())
    store.add_project_policy(
        project_id="p", name="p", strategy="trusted_local_dev", phase="build", policy={}
    )
    store.add_project_binding(project_id="p", binding_kind="repo_path", binding_value=str(project))
    pb.write_lock(config.home, template, approved_by="op")
    paths = [str(path) for path in secrets] + [str(link), "~/.ssh/id_rsa"]
    request.addfinalizer(store.close)
    return store, ConfigHolder(config, store), paths


@pytest.mark.parametrize("role", ROLES, ids=lambda r: r or "no-role")
def test_no_verdict_reads_a_secret(
    stack: tuple[RunStore, ConfigHolder, list[str]], role: str | None
) -> None:
    store, holder, paths = stack
    for path in paths:
        lane = queen_filesystem_decision(
            store,
            holder,
            action="read",
            path=path,
            role=None if role is None else SHIPPED_ROLES[role],
        )
        assert lane.verdict == "deny", (path, lane)
        record = policy_decide(
            holder, store, scope="filesystem", action="read", value=path, role=role
        )
        assert record["verdict"] == "deny", (path, record)


@pytest.mark.parametrize("role", ROLES, ids=lambda r: r or "no-role")
def test_no_queen_host_rule_installs_a_package_without_a_card(
    stack: tuple[RunStore, ConfigHolder, list[str]], role: str | None, tmp_path: Path
) -> None:
    store, holder, _ = stack
    for command in INSTALLS:
        decision = queen_shell_decision(
            store,
            holder,
            command=command,
            cwd=str(tmp_path),
            role=None if role is None else SHIPPED_ROLES[role],
        )
        assert decision is None or decision.verdict != "allow", (command, decision)
