"""v44-F6: SKILL.md import — the migration path for Hermes-style skill packs.

A pack is a directory holding ``SKILL.md`` (freeform instructions with an
optional ``---`` frontmatter block) and, often, a ``scripts/`` directory. The
converter maps it onto the existing v31 skill machinery — a
``WorkflowTemplate`` — and the CLI face (``skep skill import-md``) routes it
through the SAME human grant gate as a signed bundle import.

The trust rule: scripts are NEVER auto-granted. A script shipped in the pack
is *listed* in the disclosure, but it enters the template's shell allowlist
only via an explicit ``--allow-script`` flag — the operator types each grant.
Everything else about the imported skill is an ordinary registry template:
dispatchable, schedulable, governed by normal worker capabilities at run time.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import Path

from .templates import WorkflowTemplate, validate_template

_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*)\s*:\s*(.*)$")

# YAML block-scalar markers the Agent Skills community actually uses for
# multi-line descriptions. Folded (>) joins with spaces, literal (|) with
# newlines; the chomping variants matter only for trailing whitespace we
# strip anyway.
_BLOCK_MARKERS = {">", ">-", ">+", "|", "|-", "|+"}


def _parse_frontmatter(lines: list[str]) -> dict[str, str]:
    """The subset of YAML real SKILL.md frontmatter uses: `key: value`,
    quoted values, folded/literal block scalars, wrapped plain scalars.
    Indented lines are NEVER keys (a wrapped description containing a
    colon must not become one); unknown keys are kept and ignored by the
    caller."""
    front: dict[str, str] = {}
    key: str | None = None
    block: list[str] | None = None
    literal = False
    for raw in lines:
        if not raw.strip():
            continue
        if raw[:1] in (" ", "\t"):
            if key is not None:
                if block is not None:
                    block.append(raw.strip())
                else:  # wrapped plain scalar — continuation, not a key
                    front[key] = f"{front[key]} {raw.strip()}".strip()
            continue
        if key is not None and block is not None:
            front[key] = ("\n" if literal else " ").join(block).strip()
        key, block = None, None
        match = _KEY_RE.match(raw.strip())
        if not match:
            continue
        name, value = match.group(1).lower(), match.group(2).strip()
        if value in _BLOCK_MARKERS:
            key, block, literal = name, [], value.startswith("|")
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        front[name] = value
        key = name  # plain scalar stays open for wrapped continuation lines
    if key is not None and block is not None:
        front[key] = ("\n" if literal else " ").join(block).strip()
    return front


@dataclass(frozen=True)
class SkillMdPack:
    name: str
    description: str
    worker_kind: str
    instructions: str
    scripts_found: tuple[str, ...]
    # v100-F5 (R13): the command that proves this pack's scripts DO what they
    # say. Empty is legal and is the whole existing shelf's case — then the
    # trial is a syntax smoke and says so, rather than claiming behaviour it
    # never tested (I8).
    self_test: str = ""


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "skill"


def parse_skill_md(directory: Path) -> SkillMdPack:
    """Read ``<directory>/SKILL.md`` (+ enumerate ``scripts/``) into a pack."""
    path = directory / "SKILL.md"
    if not path.is_file():
        raise ValueError(f"no SKILL.md in {directory}")
    text = path.read_text(encoding="utf-8")

    front: dict[str, str] = {}
    body = text
    if text.startswith("---"):
        lines = text.splitlines()
        for end, line in enumerate(lines[1:], start=1):
            if line.strip() == "---":
                front = _parse_frontmatter(lines[1:end])
                body = "\n".join(lines[end + 1 :])
                break

    body = body.strip()
    if not body:
        raise ValueError(f"{path} has no instruction body")
    heading = next(
        (line[2:].strip() for line in body.splitlines() if line.startswith("# ")), ""
    )
    name = _slug(front.get("name") or directory.name)
    description = front.get("description") or heading or name
    worker_kind = front.get("worker_kind") or "coding"
    self_test = (front.get("self_test") or "").strip()

    scripts_dir = directory / "scripts"
    scripts = (
        tuple(
            sorted(
                str(item.relative_to(directory))
                for item in scripts_dir.iterdir()
                if item.is_file()
            )
        )
        if scripts_dir.is_dir()
        else ()
    )
    return SkillMdPack(
        name=name,
        description=description,
        worker_kind=worker_kind,
        instructions=body,
        scripts_found=scripts,
        self_test=self_test,
    )


def template_from_skill_md(
    pack: SkillMdPack, *, allow_scripts: tuple[str, ...] = ()
) -> WorkflowTemplate:
    """The pack as a registry skill. Only explicitly granted commands enter the
    shell allowlist; the pack's shipped scripts grant nothing by themselves."""
    shell_allowlist = tuple(tuple(shlex.split(command)) for command in allow_scripts if command)
    if any(not command for command in shell_allowlist):
        raise ValueError("--allow-script needs a non-empty command")
    template = WorkflowTemplate(
        name=pack.name,
        instructions=pack.instructions,
        description=pack.description,
        worker_kind=pack.worker_kind,
        shell_allowlist=shell_allowlist,
        provenance="imported",
    )
    validate_template(template)
    return template
