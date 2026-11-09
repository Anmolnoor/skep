"""first-run R1 (ADR 0055 D1-D3): roles as records, and the role layer.

Temp homes only."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from skep.supervisor import RunStore
from skep.supervisor.cli_cmds import build_config
from skep.supervisor.policy_registry import LAYERS
from skep.supervisor.policy_templates import TEMPLATE_NAMES, explain_view
from skep.supervisor.roles import (
    SHIPPED_ROLES,
    Role,
    all_roles,
    default_role,
    resolve_role,
    role_decision,
    roles_dir,
    tighten,
    use_role,
)
from skep.supervisor.serve.chat import validate_personality


def test_three_roles_ship_bound_one_to_one_to_the_templates() -> None:
    """Decision #6: the shipped roles ARE the template names."""
    assert tuple(SHIPPED_ROLES) == TEMPLATE_NAMES
    for name, role in SHIPPED_ROLES.items():
        assert role.policy_layer == name and role.label == f"role:{name}"
        assert role.voice is not None and validate_personality(role.voice) == role.voice
        assert role.system_prompt and role.default_castes and role.voice
        layer = role.layer()
        assert layer is not None and (layer.name, layer.label) == ("role", f"role:{name}")
        assert layer.settings == ()  # rules only: the baseline keeps the knobs


def test_the_role_slot_sits_directly_under_the_template() -> None:
    assert LAYERS[LAYERS.index("template") + 1] == "role"


def test_a_role_record_refuses_what_it_cannot_honour() -> None:
    with pytest.raises(ValueError, match="not a template"):
        Role(name="x", policy_layer="warp")
    with pytest.raises(ValueError, match="unknown caste"):
        Role(name="x", default_castes=("wizard",))
    with pytest.raises(ValueError):
        Role(name="Bad Name")


def test_custom_roles_load_from_the_home_and_refuse_naming_the_file(tmp_path: Path) -> None:
    home = tmp_path / "skep" / "supervisor"
    roles_dir(home).mkdir(parents=True)
    (roles_dir(home) / "reviewer.json").write_text(
        json.dumps({"name": "reviewer", "policy_layer": "operator", "voice": "concise"})
    )
    roles = all_roles(home)
    assert list(roles) == ["builder", "operator", "assistant", "reviewer"]
    (roles_dir(home) / "grumpy.json").write_text(json.dumps({"name": "grumpy", "voice": "grumpy"}))
    with pytest.raises(ValueError, match="unknown personality"):
        all_roles(home)
    (roles_dir(home) / "grumpy.json").unlink()
    assert resolve_role("reviewer", home).policy_layer == "operator"
    with pytest.raises(ValueError, match="no role 'warp'; known: builder, operator"):
        resolve_role("warp", home)
    (roles_dir(home) / "broken.json").write_text("{")
    with pytest.raises(ValueError, match=r"broken\.json does not load"):
        all_roles(home)


def test_the_role_layer_tightens_and_never_loosens() -> None:
    """D2: the stricter verdict stands. operator asks for pytest where the
    builder baseline allows it; builder's narrow allow cannot lift an ask."""
    operator, builder = SHIPPED_ROLES["operator"], SHIPPED_ROLES["builder"]
    allowed = {"verdict": "allow", "rule_id": "shell-pytest", "decided_by": "template:builder/x"}
    tighter = tighten(allowed, operator, "shell", "run", "pytest -q")
    assert (tighter["verdict"], tighter["decided_by"]) == (
        "require_approval",
        "role:operator/shell-gated",
    )
    asked = {"verdict": "require_approval", "rule_id": "a", "decided_by": "operator/a"}
    assert tighten(asked, builder, "shell", "run", "pytest -q") == asked
    # No rule in the role for this action: no opinion, the stack decides.
    assert role_decision(builder, "shell", "run_repo", "make") is None
    # A chained line: the least favourable matched segment decides.
    hit = role_decision(operator, "shell", "run", "git status && pytest")
    assert hit is not None and hit.rule_id == "shell-gated"
    assert tighten(allowed, None, "shell", "run", "pytest") == allowed


def test_default_role_is_the_operator_pick_then_the_onboard_one(tmp_path: Path) -> None:
    config = build_config(tmp_path / "skep", None)
    store = RunStore(config.db_path)
    try:
        assert default_role(store, config.home) is None
        (config.home.parent / "first_run.json").write_text(json.dumps({"role": "assistant"}))
        assert default_role(store, config.home) == "assistant"
        assert use_role(store, config.home, "operator") == {"default_role": "operator"}
        assert default_role(store, config.home) == "operator"
        chat = store.create_chat(title="t", model=None, role="builder")
        assert store.get_chat(chat.chat_id).role == "builder"  # type: ignore[union-attr]
        use_role(store, config.home, "assistant", chat_id=chat.chat_id)
        assert store.get_chat(chat.chat_id).role == "assistant"  # type: ignore[union-attr]
        with pytest.raises(ValueError, match="no chat"):
            use_role(store, config.home, "assistant", chat_id="nope")
        with pytest.raises(ValueError, match="no role"):
            use_role(store, config.home, "warp")
    finally:
        store.close()


def test_explain_renders_a_role_as_its_layer() -> None:
    view = explain_view("role:operator")
    assert (view["kind"], view["name"], view["layer"]) == ("role", "operator", "role")
    assert "role:operator binds template 'operator'" in view["note"]
    assert any(row["rule_id"] == "shell-gated" for row in view["rules"])
    assert "unlisted → no opinion (the baseline decides)" in view["table"]
    with pytest.raises(ValueError, match="no role 'warp'"):
        explain_view("role:warp")


# --- T2 audit: a custom role file that lies, shadows or breaks ----------------


def test_a_role_file_naming_another_role_refuses(tmp_path: Path) -> None:
    home = tmp_path / "skep" / "supervisor"
    roles_dir(home).mkdir(parents=True)
    (roles_dir(home) / "quiet.json").write_text(json.dumps({"name": "builder"}))
    with pytest.raises(ValueError, match=r"quiet\.json names itself 'builder'"):
        all_roles(home)


def test_a_file_named_like_a_shipped_role_replaces_it_and_every_reader_sees_it(
    tmp_path: Path,
) -> None:
    """The module's documented override: ~/.skep/roles/operator.json replaces
    the shipped `operator` — one table, so the layer, `explain` and the
    resolver all read the replacement, never a mix of the two."""
    home = tmp_path / "skep" / "supervisor"
    roles_dir(home).mkdir(parents=True)
    (roles_dir(home) / "operator.json").write_text(
        json.dumps({"name": "operator", "policy_layer": "assistant"})
    )
    role = resolve_role("operator", home)
    assert role.policy_layer == "assistant" and role.default_castes == ()
    assert list(all_roles(home)) == ["builder", "operator", "assistant"]
    hit = role_decision(role, "coding", "edit", "workspace")
    assert hit is not None and hit.decided_by == "role:operator/coding-denied"
