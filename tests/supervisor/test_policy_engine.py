"""P1-1 (ADR 0053 draft D2-D5): the v2 stack's semantics, pinned pure."""

from __future__ import annotations

from typing import Any

import pytest

from skep.supervisor.policy_engine import (
    Layer,
    LayerRule,
    Setting,
    resolve_stack,
)
from skep.supervisor.policy_schema import SCOPES, LearnedRule, LearnedRuleRejected


def _layer(name: str, label: str, *settings: Setting, rules: tuple[LayerRule, ...] = ()) -> Layer:
    return Layer(name=name, label=label, settings=tuple(settings), rules=rules)


def test_set_replaces_extend_unions_and_the_last_change_is_the_source() -> None:
    """The v1 shapes the golden pins: a project overlay REPLACES the global
    list, a group EXTENDS it even from below (v1 applied groups after the
    project), trusted-roots extend again, and a layer restating the
    inherited value never claims the key (v109-F9)."""
    stack = resolve_stack(
        [
            _layer(
                "global",
                "global",
                Setting("allowed_shell_commands", [["uv", "sync"]]),
                Setting("default_network", ["pypi.org"]),
                Setting("default_execution_mode", "workspace"),
                Setting("trusted_workspace_roots", ["/w"]),
            ),
            _layer("phase", "phase:maintain", Setting("default_execution_mode", "workspace")),
            _layer(
                "group",
                "group:npm-api",
                Setting("default_network", ["registry.npmjs.org"], mode="extend"),
            ),
            _layer(
                "project",
                "project",
                Setting("allowed_shell_commands", [["pytest"]]),
                Setting("default_network", ["own.example"]),
                Setting("trusted_workspace_roots", ["/p"], mode="extend"),
            ),
            _layer(
                "dispatch", "trusted-roots", Setting("trusted_workspace_roots", ["/repo"], "extend")
            ),
        ]
    )
    values = stack.values()
    assert values["allowed_shell_commands"] == [["pytest"]]
    assert values["default_network"] == ["own.example", "registry.npmjs.org"]
    assert values["trusted_workspace_roots"] == ["/w", "/p", "/repo"]
    sources = stack.sources()
    assert sources["allowed_shell_commands"] == "project"
    assert sources["default_network"] == "group:npm-api"
    assert sources["trusted_workspace_roots"] == "trusted-roots"
    assert sources["default_execution_mode"] == "global"
    mode = stack.knobs["default_execution_mode"]
    assert [entry.outcome for entry in mode.chain] == ["applied", "unchanged"]


def test_clear_renders_unresolved_until_a_higher_layer_sets_again() -> None:
    """I8: cleared is unresolved — absent from values(), named in the chain —
    never the engine's default. A higher set resolves it again."""
    stack = resolve_stack(
        [
            _layer("global", "global", Setting("default_network", ["pypi.org"])),
            _layer("project", "project", Setting("default_network", mode="clear")),
        ]
    )
    knob = stack.knobs["default_network"]
    assert not knob.resolved and knob.value is None and knob.source is None
    assert knob.cleared_at == "project"
    assert "default_network" not in stack.values()

    again = resolve_stack(
        [
            _layer("global", "global", Setting("default_network", ["pypi.org"])),
            _layer("project", "project", Setting("default_network", mode="clear")),
            _layer("dispatch", "dispatch", Setting("default_network", ["x.example"])),
        ]
    )
    knob = again.knobs["default_network"]
    assert knob.resolved and knob.value == ["x.example"] and knob.source == "dispatch"
    assert knob.cleared_at is None
    # Extending after a clear starts from nothing — the group's hosts are
    # the whole list, not the global's plus them.
    extended = resolve_stack(
        [
            _layer("global", "global", Setting("default_network", ["pypi.org"])),
            _layer("phase", "phase:build", Setting("default_network", mode="clear")),
            _layer("group", "group:g", Setting("default_network", ["g.example"], "extend")),
        ]
    )
    assert extended.values()["default_network"] == ["g.example"]
    # An extend BELOW the clear is what the clear stops: dropped, and the
    # chain says so.
    stopped = resolve_stack(
        [
            _layer("global", "global", Setting("default_network", ["pypi.org"])),
            _layer("group", "group:g", Setting("default_network", ["g.example"], "extend")),
            _layer("project", "project", Setting("default_network", mode="clear")),
        ]
    )
    knob = stopped.knobs["default_network"]
    assert not knob.resolved and knob.cleared_at == "project"
    assert [(entry.layer, entry.outcome) for entry in knob.chain] == [
        ("global", "applied"),
        ("group:g", "refused"),
        ("project", "applied"),
    ]
    assert knob.chain[1].reason == "cleared at project"


def test_enforced_refuses_every_later_statement_naming_the_layer() -> None:
    stack = resolve_stack(
        [
            _layer("global", "global", Setting("auto_apply_branch", "skep/x")),
            _layer(
                "project", "project", Setting("auto_apply_branch", "skep/maintain", enforced=True)
            ),
            _layer("session", "session", Setting("auto_apply_branch", "main")),
            _layer("dispatch", "dispatch", Setting("auto_apply_branch", mode="clear")),
        ]
    )
    knob = stack.knobs["auto_apply_branch"]
    assert knob.value == "skep/maintain" and knob.source == "project"
    assert knob.enforced_by == "project"
    refused = [entry for entry in knob.chain if entry.outcome == "refused"]
    assert [entry.layer for entry in refused] == ["session", "dispatch"]
    assert refused[0].reason == "enforced at project"


def test_a_malformed_stack_is_refused_with_the_order_named() -> None:
    with pytest.raises(ValueError, match="cannot follow 'project'"):
        resolve_stack([_layer("project", "project"), _layer("group", "group:late")])
    with pytest.raises(ValueError, match="unknown name 'floor'"):
        resolve_stack([_layer("floor", "floor")])
    with pytest.raises(ValueError, match="only a list can extend"):
        resolve_stack(
            [_layer("global", "global", Setting("default_network", "pypi.org", "extend"))]
        )
    with pytest.raises(ValueError, match="cannot extend a int"):
        resolve_stack(
            [
                _layer("global", "global", Setting("default_max_actions", 3)),
                _layer("group", "group:g", Setting("default_max_actions", [4], "extend")),
            ]
        )


def _rule(
    scope: str, verdict: str, rule_id: str, action: str, pattern: str, **kw: Any
) -> LayerRule:
    return LayerRule(scope, verdict, rule_id, action, pattern, **kw)  # type: ignore[arg-type]


def test_rules_keep_layer_order_with_sources_and_the_floor_refuses_into_the_record() -> None:
    """D5: every layer's shell allow passes the floor — a `git push` allow in
    a stored document is recorded as refused, not applied and not hidden."""
    stack = resolve_stack(
        [
            _layer(
                "global",
                "global",
                rules=(
                    _rule("shell", "allow", "shell:uv sync", "run", "uv sync"),
                    _rule("shell", "deny", "shell:rm", "run", "rm -rf"),
                    _rule("shell", "allow", "shell:push", "run", "git push origin main"),
                ),
            ),
            _layer(
                "project",
                "project",
                rules=(_rule("network", "allow", "net:pypi.org", "connect", "pypi.org"),),
            ),
        ]
    )
    shell = stack.rules["shell"]
    assert [(entry.rule.rule_id, entry.layer) for entry in shell] == [
        ("shell:uv sync", "global"),
        ("shell:rm", "global"),
    ]
    assert stack.rules["network"][0].layer == "project"
    assert [(r.layer, r.rule_id, r.reason) for r in stack.refusals] == [
        (
            "global",
            "shell:push",
            "floor/remote git commands cannot be allowlisted; skep lands patches after approval",
        )
    ]
    scopes = stack.scopes()
    assert set(scopes) == set(SCOPES)
    assert scopes["mcp"].rules == ()
    assert [rule.rule_id for rule in scopes["shell"].rules] == ["shell:uv sync", "shell:rm"]


def test_a_learned_rule_into_denied_space_raises_as_v1_does() -> None:
    learned = LearnedRule(scope="shell", rule_id="learned:echo", action="run", pattern="echo hi")
    with pytest.raises(LearnedRuleRejected, match="shell:echo"):
        resolve_stack(
            [
                _layer(
                    "global",
                    "global",
                    rules=(_rule("shell", "deny", "shell:echo", "run", "echo"),),
                ),
                _layer(
                    "session",
                    "learned",
                    rules=(
                        _rule(
                            "shell",
                            "allow",
                            "learned:echo",
                            "run",
                            "echo hi",
                            learned=True,
                            origin=learned,
                        ),
                    ),
                ),
            ]
        )
