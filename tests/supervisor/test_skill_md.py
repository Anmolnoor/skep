"""v44-F6: SKILL.md pack import — Hermes-pack migration through the v31 gate.

The trust pin: shipped scripts grant NOTHING; only explicit --allow-script
commands enter the shell allowlist, and they show up in the grant disclosure.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from skep.supervisor import RunStore
from skep.supervisor.skill_bundle import skill_grants
from skep.supervisor.skill_cmds import cmd_skill_import_md
from skep.supervisor.skill_md import parse_skill_md, template_from_skill_md


def _pack(tmp_path: Path, *, frontmatter: str = "", scripts: tuple[str, ...] = ()) -> Path:
    directory = tmp_path / "homelab-control"
    directory.mkdir()
    body = "# Homelab control\n\nQuery Proxmox and Pi-hole via the shipped scripts."
    (directory / "SKILL.md").write_text(f"{frontmatter}{body}\n", encoding="utf-8")
    if scripts:
        (directory / "scripts").mkdir()
        for script in scripts:
            (directory / "scripts" / script).write_text("#!/bin/sh\necho ok\n")
    return directory


def test_parse_skill_md_reads_frontmatter_body_and_scripts(tmp_path: Path) -> None:
    directory = _pack(
        tmp_path,
        frontmatter="---\nname: Homelab Control\ndescription: homelab queries\n---\n",
        scripts=("proxmox_api.sh", "pihole_api.sh"),
    )
    pack = parse_skill_md(directory)
    assert pack.name == "homelab-control"
    assert pack.description == "homelab queries"
    assert pack.worker_kind == "coding"
    assert pack.scripts_found == ("scripts/pihole_api.sh", "scripts/proxmox_api.sh")
    assert pack.instructions.startswith("# Homelab control")
    assert "---" not in pack.instructions


def test_parse_skill_md_handles_agent_skills_frontmatter(tmp_path: Path) -> None:
    """v85-F1: the Agent Skills standard's real-world YAML idioms — quoted
    values, folded block scalars, wrapped lines (with colons) that must never
    become keys, unknown keys ignored."""
    directory = _pack(
        tmp_path,
        frontmatter=(
            "---\n"
            'name: "Homelab Control"\n'
            "description: >-\n"
            "  Query Proxmox and Pi-hole.\n"
            "  Use when: the user asks about the homelab.\n"
            "license: Apache-2.0\n"
            "allowed-tools: Bash, Read\n"
            "metadata:\n"
            "  author: someone\n"
            "---\n"
        ),
    )
    pack = parse_skill_md(directory)
    assert pack.name == "homelab-control"
    assert (
        pack.description == "Query Proxmox and Pi-hole. Use when: the user asks about the homelab."
    )
    assert pack.worker_kind == "coding"  # "use when:" line did not become a key
    assert pack.instructions.startswith("# Homelab control")


def test_parse_skill_md_wrapped_plain_scalar_and_literal_block(tmp_path: Path) -> None:
    directory = _pack(
        tmp_path,
        frontmatter=(
            "---\n"
            "description: a long description\n"
            "  that wraps onto a second line\n"
            "notes: |-\n"
            "  line one\n"
            "  line two\n"
            "---\n"
        ),
    )
    pack = parse_skill_md(directory)
    assert pack.description == "a long description that wraps onto a second line"


def test_parse_skill_md_defaults_and_refusals(tmp_path: Path) -> None:
    directory = _pack(tmp_path)  # no frontmatter, no scripts
    pack = parse_skill_md(directory)
    assert pack.name == "homelab-control"  # directory name, slugged
    assert pack.description == "Homelab control"  # first heading
    assert pack.scripts_found == ()

    with pytest.raises(ValueError, match=r"no SKILL\.md"):
        parse_skill_md(tmp_path / "missing")
    empty = tmp_path / "empty"
    empty.mkdir()
    (empty / "SKILL.md").write_text("---\nname: x\n---\n   \n")
    with pytest.raises(ValueError, match="no instruction body"):
        parse_skill_md(empty)


def test_scripts_grant_nothing_unless_explicitly_allowed(tmp_path: Path) -> None:
    pack = parse_skill_md(_pack(tmp_path, scripts=("proxmox_api.sh",)))
    ungranted = template_from_skill_md(pack)
    assert ungranted.shell_allowlist == ()
    assert skill_grants(ungranted)["dangerous"] is False

    granted = template_from_skill_md(pack, allow_scripts=("scripts/proxmox_api.sh cluster",))
    assert granted.shell_allowlist == (("scripts/proxmox_api.sh", "cluster"),)
    grants = skill_grants(granted)
    assert grants["dangerous"] is True
    assert grants["shell_commands"] == [["scripts/proxmox_api.sh", "cluster"]]


def test_import_md_cli_is_review_first_then_admits(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = _pack(tmp_path, scripts=("proxmox_api.sh",))
    home = tmp_path / "home"

    review = cmd_skill_import_md(
        argparse.Namespace(
            directory=str(directory), allow_script=[], approve=False, as_name=None, home=home
        )
    )
    assert review == 0
    out = capsys.readouterr().out
    assert "nothing imported" in out
    assert "scripts/proxmox_api.sh  [not granted]" in out

    # v85-F3: requesting a script grant makes the pack a PACKAGE — it drafts
    # onto the v17 ladder instead of entering the registry directly.
    admitted = cmd_skill_import_md(
        argparse.Namespace(
            directory=str(directory),
            allow_script=["scripts/proxmox_api.sh"],
            approve=True,
            as_name=None,
            home=home,
        )
    )
    assert admitted == 0
    out = capsys.readouterr().out
    assert "drafted: skill pack" in out and "skep skill promote" in out
    store = RunStore(home.expanduser().resolve() / "supervisor" / "supervisor.sqlite3")
    try:
        assert store.get_template("homelab-control") is None  # ladder, not registry
        from skep.supervisor.skill_packs import load_packs

        record = load_packs(store)["homelab-control"]
        assert record.state == "draft"
        assert record.grants == ("scripts/proxmox_api.sh",)
    finally:
        store.close()

    # Zero-grant import (no --allow-script) still admits directly.
    direct = cmd_skill_import_md(
        argparse.Namespace(
            directory=str(directory), allow_script=[], approve=True, as_name=None, home=home
        )
    )
    assert direct == 0

    # No silent overwrite on a second direct import.
    again = cmd_skill_import_md(
        argparse.Namespace(
            directory=str(directory), allow_script=[], approve=True, as_name=None, home=home
        )
    )
    assert again != 0
