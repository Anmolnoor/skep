"""first-run O1 (ADR 0056 D3): three templates in the layer format, pinned.

Per template: the file validates as one engine v2 layer, its resolution
matches the committed golden byte-for-byte (a verdict or knob change without
a doc change fails here), the P0 harness freezes the same answers with the
template as the baseline whether every scope runs on v1 or v2, nothing it
sets is outside the registry's catalogue, nothing it grants reaches the
floor, and its documented character holds. Under `builder` the B3 demo
sequence holds through the real entry point every face uses
(``policy_decide``): `pnpm test` allow · `rm` ask · `git push` deny. The four
ADR 0022 names resolve as aliases and say so. `explain` renders the same
words on three faces."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from skep.cli import main
from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor.cli_cmds import build_config
from skep.supervisor.policy_engine import resolve_stack
from skep.supervisor.policy_golden import HOME_PLACEHOLDER, SEED_VERSION, freeze
from skep.supervisor.policy_hook import hook_output
from skep.supervisor.policy_registry import registry_keys, registry_lookup
from skep.supervisor.policy_schema import (
    SCOPE_ACTIONS,
    SCOPES,
    LearnedRule,
    LearnedRuleRejected,
    PolicyDocument,
    decide,
    resolve,
    vet_learned_rule,
)
from skep.supervisor.policy_templates import (
    TEMPLATE_ALIASES,
    TEMPLATE_LAYER,
    TEMPLATE_NAMES,
    builtin_policy_templates,
    explain_view,
    floor_rows,
    golden_bytes,
    load_policy_template,
    render_policy_table,
    resolve_template_name,
)
from skep.supervisor.serve.actions import policy_decide
from skep.supervisor.serve.settings import ConfigHolder
from skep.supervisor.serve.tools import execute_read_tool
from skep.supervisor.shell_prefixes import dangerous_prefix_reason

from .conftest import serve_client

GOLDEN_DIR = Path(__file__).parents[1] / "fixtures" / "policy_templates"


def _resolved(name: str) -> Any:
    return resolve(load_policy_template(name).document())


# --- the pins ---------------------------------------------------------------


@pytest.mark.parametrize("name", TEMPLATE_NAMES)
def test_template_resolution_matches_its_golden(name: str) -> None:
    template = load_policy_template(name)
    golden = (GOLDEN_DIR / f"{name}.resolved.json").read_text(encoding="utf-8")
    assert golden_bytes(template) == golden, (
        f"{name} drifted — if the change is intended, regenerate the golden AND "
        "update the documented character in the same commit"
    )


def test_all_three_load_and_reference_real_packs() -> None:
    templates = builtin_policy_templates()
    assert set(templates) == set(TEMPLATE_NAMES) == {"builder", "operator", "assistant"}
    assert templates["builder"].pack == "trusted_local_dev"
    assert templates["operator"].pack == "trusted_local_ops"
    assert templates["assistant"].pack is None
    assert not (GOLDEN_DIR / "locked-down.resolved.json").exists(), "locked-down has no file (D2)"


@pytest.mark.parametrize("name", TEMPLATE_NAMES)
def test_golden_parity_with_the_template_as_the_baseline(name: str, tmp_path: Path) -> None:
    """(a) The P0 harness, the template applied the way `skep setup --template`
    applies it (document + knobs), one bound project: every scope on v1 and
    every scope on v2 freeze to the same golden."""
    template = load_policy_template(name)
    seed: dict[str, Any] = {
        "version": SEED_VERSION,
        "settings": {**template.settings, "default_execution_mode": "workspace"},
        "policy_document": template.document().model_dump(mode="json"),
        "operator_policy_document": None,
        "policy_groups": {},
        "projects": [
            {
                "project_id": "app",
                "name": "app",
                "strategy": template.pack or "trusted_local_dev",
                "phase": "build",
                "policy": {},
                "pack_name": None,
                "pack_version": None,
                "bindings": [{"kind": "repo_path", "value": f"{HOME_PLACEHOLDER}/bound/app"}],
            }
        ],
        "templates": [],
    }

    def frozen(scopes: list[str], home: Path) -> dict[str, Any]:
        golden = freeze(
            {**seed, "settings": {**seed["settings"], "policy_engine_v2_scopes": scopes}}, home
        )
        text = json.dumps(golden, sort_keys=True)
        return dict(json.loads(text))

    v1 = frozen([], tmp_path / "v1")
    v2 = frozen(list(SCOPES), tmp_path / "v2")
    for golden in (v1, v2):
        project = golden["project-app"]
        project["run_policy"].pop("policy_engine_v2_scopes", None)
        project["run_policy"]["_provenance"].pop("policy_engine_v2_scopes", None)
    assert v1 == v2, f"{name}: engine v2 answers differently with the template as baseline"
    # The template's rules ARE the operator's base (the operator overlay adds
    # only its own `net:search`), and its knobs are the run's.
    operator_ids = {
        rule["rule_id"] for rules in v1["operator"]["scopes"].values() for rule in rules
    }
    assert operator_ids - {"net:search"} == {rule.rule_id for rule in template.rules}
    # (the strategy's own registry hosts merge in beside the template's)
    assert set(template.settings["default_network"]) <= set(
        v1["project-app"]["resolved"]["network_resolved"]
    )


@pytest.mark.parametrize("name", TEMPLATE_NAMES)
def test_no_template_sets_a_key_outside_the_registry_catalogue(name: str) -> None:
    """(b) Every knob is a registry key the `template` layer may carry; every
    rule is a registry rule key (scope.action) that lists `template`."""
    template = load_policy_template(name)
    assert template.settings, "a complete baseline candidate states its knobs"
    for key in template.settings:
        entry = registry_lookup(key)
        assert entry is not None and entry.kind == "knob", key
        assert TEMPLATE_LAYER in entry.layers, key
    for rule in template.rules:
        assert rule.action in SCOPE_ACTIONS[rule.scope]
        entry = registry_lookup(f"{rule.scope}.{rule.action}")
        assert entry is not None and entry.kind == "rule", rule.rule_id
        assert TEMPLATE_LAYER in entry.layers, rule.rule_id
    # The knobs say the same thing the rules do (one vocabulary, I12).
    shell_allows = {
        rule.pattern for rule in template.rules if rule.scope == "shell" and rule.verdict == "allow"
    }
    assert {" ".join(argv) for argv in template.settings["allowed_shell_commands"]} == shell_allows
    hosts = {
        rule.pattern
        for rule in template.rules
        if rule.scope == "network" and rule.action == "connect" and rule.verdict == "allow"
    }
    assert set(template.settings["default_network"]) == hosts


@pytest.mark.parametrize("name", TEMPLATE_NAMES)
def test_no_template_grants_a_floor_item(name: str) -> None:
    """(c) `vet_learned_rule`'s shape over every allow and ask: no shell
    pattern reaches a never-grantable prefix, the v2 stack records no
    refusal, and a learned rule lifting `git push` is rejected under every
    template (asserted against the resolver, not the docs)."""
    template = load_policy_template(name)
    for rule in template.rules:
        if rule.verdict == "deny":
            continue
        vet_learned_rule(
            LearnedRule(
                rule_id=rule.rule_id, action=rule.action, pattern=rule.pattern, scope=rule.scope
            ),
            [],
        )
    for argv in template.settings["allowed_shell_commands"]:
        assert dangerous_prefix_reason(list(argv)) is None, argv
    stack = resolve_stack([template.layer()])
    assert stack.refusals == ()
    assert set(stack.values()) == set(template.settings)
    pushy = LearnedRule(rule_id="pushy", action="run", pattern="git push", scope="shell")
    with pytest.raises(LearnedRuleRejected) as excinfo:
        resolve(template.document(), learned=(pushy,))
    assert excinfo.value.deny_rule_id.startswith("floor/")
    # Loading a file that reaches into the floor refuses at the door.
    bad = template.model_dump(mode="json")
    bad["rules"].append(
        {
            "scope": "shell",
            "verdict": "allow",
            "action": "run",
            "pattern": "git push",
            "rule_id": "x",
        }
    )
    with pytest.raises(ValueError, match="remote git"):
        type(template).model_validate(bad)
    worse = template.model_dump(mode="json")
    worse["settings"]["auto_apply_verified_patch"] = True
    with pytest.raises(ValueError, match="does not let the 'template' layer"):
        type(template).model_validate(worse)


def _parts(home: Path) -> tuple[RunStore, ConfigHolder]:
    config = build_config(home, None)
    store = RunStore(config.db_path)
    return store, ConfigHolder(config, store)


def test_the_b3_demo_sequence_holds_under_builder(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """(d) Through `policy_decide`, the one entry the hook, the governor and
    the REST face share: `pnpm test` allow · `rm -rf build` ask · `git push`
    deny — and the hook renders those three words. O5: a fresh home asks
    (``no_baseline``) until the baseline is approved, so the demo approves it."""
    home = tmp_path / "home"
    assert main(["--home", str(home), "setup", "--template", "builder"]) == 0
    assert main(["--home", str(home), "policy", "approve", "--template", "builder", "--yes"]) == 0
    capsys.readouterr()
    store, holder = _parts(home)
    try:
        ran = policy_decide(holder, store, scope="shell", action="run", value="pnpm test")
        asked = policy_decide(holder, store, scope="shell", action="run", value="rm -rf build")
        denied = policy_decide(
            holder, store, scope="shell", action="run", value="git push origin main"
        )
    finally:
        store.close()
    assert (ran["verdict"], ran["rule_id"]) == ("allow", "shell-pnpm-test")
    assert (asked["verdict"], asked["rule_id"]) == ("require_approval", "shell-learnable")
    assert (denied["verdict"], denied["rule_id"]) == ("deny", "floor.worker-git-hard-denies")
    decisions = [
        hook_output(record)["hookSpecificOutput"]["permissionDecision"]
        for record in (ran, asked, denied)
    ]
    assert decisions == ["allow", "ask", "deny"]
    # The rule names explain prints are the ones the decisions carry.
    explained = {row["rule_id"] for row in explain_view("builder")["rules"]}
    floor = {row["rule_id"] for row in explain_view("builder")["floor"]}
    assert {ran["rule_id"], asked["rule_id"]} <= explained
    assert denied["rule_id"] in floor


@pytest.mark.parametrize(("alias", "new"), sorted(TEMPLATE_ALIASES.items()))
def test_each_alias_resolves_and_prints_the_new_name(
    alias: str, new: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """(e) ADR 0056 D3: the ADR 0022 names work for one release and say what
    they became."""
    assert resolve_template_name(alias) == (new, alias)
    assert load_policy_template(alias).template == new
    view = explain_view(alias)
    assert (view["name"], view["deprecated_alias"]) == (new, alias)
    assert main(["--home", str(tmp_path / "home"), "setup", "--template", alias, "--dry-run"]) == 0
    captured = capsys.readouterr()
    assert f"policy template: {new}" in captured.out
    assert f"'{alias}' is now '{new}'" in captured.err
    assert "1.3.0" in captured.err


def test_unknown_names_teach_the_three() -> None:
    with pytest.raises(ValueError, match="builder, operator, assistant"):
        load_policy_template("warp")
    assert set(TEMPLATE_ALIASES) == {"personal-dev", "homelab-ops", "locked-down"}
    assert set(TEMPLATE_ALIASES.values()) <= set(TEMPLATE_NAMES)


# --- the characters -----------------------------------------------------------


def test_builder_character_workspace_free_toolchain_runs_rest_asks() -> None:
    resolved = _resolved("builder")
    assert decide(resolved, "coding", "edit", "workspace").verdict == "allow"
    assert decide(resolved, "shell", "run", "git status --short").verdict == "allow"
    assert decide(resolved, "shell", "run", "uv run pytest -q").verdict == "allow"
    assert decide(resolved, "shell", "run", "cargo build").verdict == "require_approval"
    assert decide(resolved, "filesystem", "write", "workspace").verdict == "allow"
    assert decide(resolved, "filesystem", "write", "/etc/hosts").verdict == "require_approval"
    assert decide(resolved, "network", "connect", "pypi.org").verdict == "allow"
    assert decide(resolved, "network", "connect", "example.com").verdict == "require_approval"
    assert decide(resolved, "mcp", "call", "srv:tool").verdict == "require_approval"


def test_operator_character_inspection_runs_toolchain_asks_github_fetch_flows() -> None:
    resolved = _resolved("operator")
    assert decide(resolved, "shell", "run", "git log --oneline").verdict == "allow"
    assert decide(resolved, "shell", "run", "df -h").verdict == "allow"
    # builder minus arbitrary shell: the toolchain executes repository code.
    assert decide(resolved, "shell", "run", "pnpm test").verdict == "require_approval"
    assert decide(resolved, "shell", "run", "systemctl status nginx").verdict == "require_approval"
    assert decide(resolved, "network", "fetch", "api.github.com").verdict == "allow"
    assert decide(resolved, "network", "fetch", "example.com").verdict == "require_approval"
    assert decide(resolved, "filesystem", "write", "/srv/other-repo/x").verdict == (
        "require_approval"
    )
    assert decide(resolved, "email", "send", "mail:send_message").verdict == "require_approval"


def test_assistant_character_no_shell_personal_tools_flow_repo_writes_denied() -> None:
    resolved = _resolved("assistant")
    assert decide(resolved, "shell", "run", "ls").verdict == "require_approval"
    assert decide(resolved, "coding", "edit", "workspace").verdict == "deny"
    assert decide(resolved, "filesystem", "write", "workspace").verdict == "deny"
    assert decide(resolved, "filesystem", "write", "/notes/today.md").verdict == (
        "require_approval"
    )
    assert decide(resolved, "filesystem", "read", "/notes/today.md").verdict == "allow"
    assert decide(resolved, "mcp", "call", "calendar:list_events").verdict == "allow"
    assert decide(resolved, "mcp", "call", "notes:append").verdict == "allow"
    assert decide(resolved, "mcp", "call", "srv:tool").verdict == "require_approval"
    assert decide(resolved, "email", "read", "mail:read_inbox").verdict == "allow"
    assert decide(resolved, "email", "send", "mail:send_message").verdict == "require_approval"
    assert decide(resolved, "browse", "act", "web:click").verdict == "require_approval"
    settings = load_policy_template("assistant").settings
    assert settings == {"default_network": [], "allowed_shell_commands": []}


def test_overlay_composition_tightens_a_template() -> None:
    """'builder with stricter mcp' is an overlay, not a fork."""
    base = load_policy_template("builder").document()
    overlay = PolicyDocument.model_validate(
        {
            "scopes": [
                {"scope": "mcp", "deny": [{"rule_id": "no-mcp", "action": "call", "pattern": "*"}]}
            ]
        }
    )
    resolved = resolve(base, overlays=(overlay,))
    assert decide(resolved, "mcp", "call", "srv:tool").verdict == "deny"
    assert decide(resolved, "coding", "edit", "workspace").verdict == "allow"


# --- explain: one table, three faces -------------------------------------------


def test_explain_is_deterministic_grouped_by_scope_with_the_floor_labelled() -> None:
    view = explain_view("builder")
    rows = view["rules"]
    scopes = [row["scope"] for row in rows]
    assert scopes == sorted(scopes, key=SCOPES.index), "grouped by scope, in the engine's order"
    for scope in set(scopes):
        verdicts = [row["verdict"] for row in rows if row["scope"] == scope]
        order = {"allow": 0, "ask": 1, "deny": 2}
        assert verdicts == sorted(verdicts, key=order.__getitem__), scope
    assert {row["verdict"] for row in rows} <= {"allow", "ask", "deny"}
    # Every registry floor key is a row, labelled, never a template row.
    assert [row["rule_id"] for row in view["floor"]] == [
        key.name for key in registry_keys(system="floor")
    ]
    assert {row["label"] for row in view["floor"]} == {"always denied", "always asks"}
    assert all(row["label"] == "always denied" for row in view["floor"] if row["verdict"] == "deny")
    assert view["settings"] == [
        {
            "scope": "shell",
            "key": "allowed_shell_commands",
            "value": load_policy_template("builder").settings["allowed_shell_commands"],
            "layer": "template",
        },
        {
            "scope": "network",
            "key": "default_network",
            "value": load_policy_template("builder").settings["default_network"],
            "layer": "template",
        },
    ]
    table = view["table"]
    assert table == render_policy_table(view) == explain_view("builder")["table"]
    lines = table.splitlines()
    assert lines[0] == "policy template: builder  (pack: trusted_local_dev)"
    assert lines[2].startswith("| scope ") and set(lines[3]) <= set("|-")
    assert any("| floor.worker-git-hard-denies — always denied" in line for line in lines)
    assert any("| ask     | run     | *" in line and "shell-learnable" in line for line in lines)
    assert "settings:" in lines
    assert any("| allowed_shell_commands |" in line and "pnpm test" in line for line in lines)
    assert all(not line.startswith("|") or line.endswith("|") for line in lines), "pipe table"


def test_explain_answers_none_yet_for_baseline(tmp_path: Path) -> None:
    baseline = explain_view("baseline", tmp_path / "fresh")
    assert baseline["kind"] == "baseline" and baseline["name"] is None
    assert baseline["note"] == "baseline: none approved"
    assert baseline["status"]["state"] == "none"
    assert baseline["rules"] == [] and baseline["floor"] == floor_rows()
    assert "policy baseline: (none)" in baseline["table"]
    with pytest.raises(ValueError, match="no policy template"):
        explain_view("warp")


def test_explain_on_three_faces_says_the_same_words(
    config: SupervisorConfig, capsys: pytest.CaptureFixture[str]
) -> None:
    """ADR 0050: CLI, REST and chat land on `explain_view`; the rendered table
    is identical on each, and the CLI's paste is the table and nothing else."""
    view = explain_view("builder")
    assert main(["--home", str(config.home.parent), "policy", "explain", "builder"]) == 0
    out = capsys.readouterr()
    assert out.out == view["table"] + "\n"
    assert out.err == ""  # a canonical name prints no deprecation line
    assert main(["--home", str(config.home.parent), "policy", "explain", "personal-dev"]) == 0
    out = capsys.readouterr()
    assert out.out == view["table"] + "\n"
    assert "'personal-dev' is now 'builder'" in out.err
    assert main(["--home", str(config.home.parent), "policy", "explain", "warp"]) == 2

    client = serve_client(config)
    rest = client.get("/api/policy/explain", params={"subject": "builder"})
    assert rest.status_code == 200 and rest.json() == view
    assert client.get("/api/policy/explain", params={"subject": "warp"}).status_code == 404
    assert client.get("/api/policy/explain", params={"subject": "baseline"}).json()["kind"] == (
        "baseline"
    )

    store = RunStore(config.db_path)
    try:
        chat = execute_read_tool(
            "policy_explain",
            {"subject": "builder"},
            store=store,
            holder=ConfigHolder(config, store),
        )
    finally:
        store.close()
    assert chat == view


# --- T1: the door a hand-written layer comes through ----------------------------


def _builder_with(mutate: Any) -> dict[str, Any]:
    layer = load_policy_template("builder").model_dump(mode="json")
    mutate(layer)
    return layer


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda d: d["settings"].update(frobnicate=True), "unknown knob 'frobnicate'"),
        (
            lambda d: d["settings"]["allowed_shell_commands"].append(["sudo", "make"]),
            "sudo make",
        ),
        (
            lambda d: d["rules"].append(
                {"scope": "shell", "verdict": "allow", "action": "edit", "rule_id": "x"}
            ),
            "scope 'shell' has no action 'edit'",
        ),
        (lambda d: d["rules"].append(dict(d["rules"][0])), "repeats a rule_id"),
    ],
    ids=["unknown-knob", "floor-prefix-in-a-knob", "action-outside-scope", "repeated-rule-id"],
)
def test_a_layer_file_outside_the_catalogue_is_refused_at_the_door(
    mutate: Any, match: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """D3: what `skep policy approve --file` accepts is a template — a knob the
    registry lacks, a floor prefix smuggled in through the shell allowlist
    knob (not a rule), an action its scope does not have, or a rule_id that
    two rules share (the diff and `why` key on it) never reach the lock."""
    layer = _builder_with(mutate)
    with pytest.raises(ValueError, match=match):
        type(load_policy_template("builder")).model_validate(layer)
    path = tmp_path / "layer.json"
    path.write_text(json.dumps(layer))
    argv = ["--home", str(tmp_path), "policy", "approve", "--file", str(path), "--yes"]
    assert main(argv) != 0
    assert match in capsys.readouterr().err
    assert not (build_config(tmp_path, None).home / "policy" / "baseline.json").exists()


def test_a_shipped_file_must_name_itself_and_a_real_pack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A packaging slip (operator.json saved over builder.json, a renamed
    pack) fails the load instead of locking the wrong template."""
    from skep.supervisor import policy_templates

    renamed = load_policy_template("operator").model_dump(mode="json")
    packless = _builder_with(lambda d: d.update(pack="no-such-pack"))
    monkeypatch.setattr(policy_templates, "TEMPLATES_DIR", tmp_path)
    (tmp_path / "builder.json").write_text(json.dumps(renamed))
    with pytest.raises(ValueError, match=r"builder\.json names itself 'operator'"):
        load_policy_template("builder")
    (tmp_path / "builder.json").write_text(json.dumps(packless))
    with pytest.raises(ValueError, match="unknown pack 'no-such-pack'"):
        load_policy_template("personal-dev")  # the alias takes the same door
