"""ES-F6 (edge sweep §5.4, items 1, 3 and 6): forbidden combinations, as a
property over every shipped template, role, engine and learned-grant tier.

1. No reachable verdict allows reading a secret-floor path.
3. No Queen host-lane rule lets a package install or a networked command
   run without a card — a grant (P3-2: it answers a role's catch-all ask)
   covers only the exact line a human approved.
6. A learned rule that lands inside a deny at any layer is ignored at decide
   time (and named in the decision log), never honoured and never raised.

Each template reaches a home two ways at once — the global document
`skep setup --template` stores and the approved baseline — the widest
stack the shipped policy can build. Imports only pre-existing faces on
purpose, so the same file fails against the tree before ES-F1/ES-F3.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

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
GRANTS = ("none", "session", "always")
GRANTED_INSTALL = "npm install left-pad@1.3.0"  # the one install a human approved
NETWORKED = (
    "curl https://x.example",
    "wget https://x.example",
    "ssh host.example",
    "scp a.txt host.example:b",
    "nc host.example 80",
    "rsync -a . host.example:x",
)
HOME_SECRETS = (
    ".ssh/id_rsa",
    ".aws/credentials",
    ".gnupg/private-keys-v1.d/key",
    ".config/gh/hosts.yml",
    ".netrc",
    ".git-credentials",
    ".docker/config.json",
    ".kube/config",
    ".secrets/gmail-app-password",
    ".config/gcloud/credentials.db",
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


def _doc(store: RunStore) -> dict[str, Any]:
    raw = store.get_setting(POLICY_DOCUMENT_SETTINGS_KEY)
    return json.loads(raw) if isinstance(raw, str) else dict(raw or {})


@pytest.fixture(
    params=[(t, e, g) for t in TEMPLATE_NAMES for e in ENGINES for g in GRANTS],
    ids=lambda p: "-".join(p),
)
def stack(
    request: pytest.FixtureRequest,
    config: SupervisorConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[RunStore, ConfigHolder, list[str]]:
    template_name, engine, grant = request.param
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
    if grant != "none":
        from skep.supervisor.serve.actions import remember_action_for_session

        assert remember_action_for_session(
            store, tool="run_shell", args={"command": GRANTED_INSTALL}, actor="op", tier=grant
        )
        # A grant written before the floor existed, inside it: still a deny.
        document = template.document().model_dump(mode="json")
        document["learned"] = [
            *(_doc(store).get("learned") or []),
            {"rule_id": "fs:all", "scope": "filesystem", "action": "read", "pattern": "*",
             "provenance": "allow-always:op"},
        ]  # fmt: skip
        store.set_setting(POLICY_DOCUMENT_SETTINGS_KEY, json.dumps(document))
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
    for command in (*INSTALLS, *NETWORKED):
        decision = queen_shell_decision(
            store,
            holder,
            command=command,
            cwd=str(tmp_path),
            role=None if role is None else SHIPPED_ROLES[role],
        )
        assert decision is None or decision.verdict != "allow", (command, decision)


NO_CURL = {"scope": "shell", "verdict": "deny", "action": "run", "pattern": "curl",
           "rule_id": "no-curl"}  # fmt: skip


@pytest.mark.parametrize("template_name", TEMPLATE_NAMES)
@pytest.mark.parametrize("role", ROLES, ids=lambda r: r or "no-role")
@pytest.mark.parametrize("tier", ["session", "always"])
def test_a_learned_rule_inside_a_deny_is_ignored_never_honoured_never_raised(
    config: SupervisorConfig, tmp_path: Path, template_name: str, role: str | None, tier: str
) -> None:
    """§5.4 #6 (B23): a grant stored before the baseline denied its command —
    every lane still decides (no LearnedRuleRejected), the deny decides that
    command, and the decision log names the ignored grant."""
    from skep.supervisor.serve.actions import remember_action_for_session

    store = RunStore(predating_home(config.db_path))
    try:
        assert remember_action_for_session(
            store, tool="run_shell", args={"command": "curl https://x.example/y"}, actor="op",
            tier=tier,
        )  # fmt: skip
        layer = load_policy_template(template_name).model_dump(mode="json")
        layer["rules"] = [*layer["rules"], NO_CURL]
        pb.write_lock(config.home, pb._load_layer(layer), approved_by="op")
        holder = ConfigHolder(config, store)
        chat_role = None if role is None else SHIPPED_ROLES[role]
        curl = queen_shell_decision(
            store, holder, command="curl https://x.example/y", cwd=str(tmp_path), role=chat_role
        )
        assert curl is not None
        assert (curl.verdict, curl.decided_by) == ("deny", f"template:{template_name}/no-curl")
        other = queen_shell_decision(store, holder, command="ls", cwd=str(tmp_path), role=chat_role)
        assert other is not None and other.verdict != "allow"
        read = queen_filesystem_decision(
            store, holder, action="read", path=str(config.home), role=chat_role
        )
        assert read.verdict in ("allow", "require_approval")
    finally:
        store.close()
    rows = (config.audit_dir / "policy" / "decisions.jsonl").read_text().splitlines()
    assert (
        "shell:run:curl https://x.example/y (deny/no-curl)"
        in json.loads(rows[-1])["ignored_grants"]
    )
