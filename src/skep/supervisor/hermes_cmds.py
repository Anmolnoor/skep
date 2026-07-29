"""v84-F8: ``skep hermes import`` — the migration, inverted.

Hermes ships an exporter; what this operator needs is the other direction:
pull memory, skills, and session history OUT of ``~/.hermes`` so it can be
archived (standing note since v44). Everything stages behind skep's
EXISTING gates — nothing becomes durable by importing:

- memory facts  -> memory proposals (``pending_review``; the approve pass
  is the only path to a durable item, exactly as for the curator),
- skills        -> skill candidates in ``draft`` (they walk the
  draft->tested->approved machine; shipped scripts are never granted),
- sessions      -> read-only transcripts with ``source='hermes-import'``
  (I8 — provenance visible in every search hit, ranked below native).

The import itself is one explicit CLI act by the operator at the keyboard
(the ``skep skill import-md`` trust shape); ``--dry-run`` prints the full
manifest and mutates nothing. A5 (review): the manifest groups per memory
class and the batch review verbs live in ``skep memory
approve-batch``/``reject-batch`` so a 400-item queue never gets
rubber-stamped one card at a time.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .memory import MemoryError
from .skill_md import parse_skill_md, template_from_skill_md
from .skills import DRAFT, SkillCandidate, candidate_signature
from .store import RunStore

IMPORT_ACTOR = "hermes-import"
CHAT_SOURCE = "hermes-import"

# Hermes memory types that map onto skep's classes; anything unrecognized
# lands honestly as a project_fact proposal the reviewer can reclassify by
# rejecting and re-proposing (ponytail: no interactive remap flow).
_CLASS_ALIASES = {
    "durable_preference": "durable_preference",
    "preference": "durable_preference",
    "user": "durable_preference",
    "feedback": "policy_hint",
    "policy_hint": "policy_hint",
    "project": "project_fact",
    "project_fact": "project_fact",
    "reference": "project_fact",
    "todo": "todo",
    "not_to_do": "not_to_do",
    "reminder": "reminder",
}
_DEFAULT_CLASS = "project_fact"


def _split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """(frontmatter, body) from an optional leading ``---`` block. Only flat
    ``key: value`` lines are read — nested metadata keys count too."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text.strip()
    meta: dict[str, str] = {}
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return meta, "\n".join(lines[index + 1 :]).strip()
        if ":" in line:
            key, _, value = line.partition(":")
            meta[key.strip().lower()] = value.strip()
    return {}, text.strip()  # unterminated block: treat as body


def _memory_class(meta: dict[str, str]) -> str:
    raw = (meta.get("type") or meta.get("class") or "").strip().lower()
    return _CLASS_ALIASES.get(raw, _DEFAULT_CLASS)


def _read_memory(home: Path) -> list[tuple[str, str]]:
    """(memory_class, content) per fact file under ``memory/``."""
    facts: list[tuple[str, str]] = []
    directory = home / "memory"
    if not directory.is_dir():
        return facts
    for path in sorted(directory.glob("*.md")):
        if path.name.upper() == "MEMORY.MD":  # the index, not a fact
            continue
        try:
            meta, body = _split_frontmatter(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue
        if body:
            facts.append((_memory_class(meta), body))
    return facts


def _read_sessions(home: Path) -> list[tuple[str, list[tuple[str, str]]]]:
    """(title, [(role, content), ...]) per ``sessions/*.jsonl`` transcript.
    Tolerant line reader: role/content or type/text keys; only what a human
    said or read (user/assistant) — tool traffic is noise, same rule as
    search_chats."""
    sessions: list[tuple[str, list[tuple[str, str]]]] = []
    directory = home / "sessions"
    if not directory.is_dir():
        return sessions
    for path in sorted(directory.glob("*.jsonl")):
        messages: list[tuple[str, str]] = []
        try:
            raw_lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        for raw in raw_lines:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            role = str(obj.get("role") or obj.get("type") or "")
            content = obj.get("content") if "content" in obj else obj.get("text")
            if role in ("user", "assistant") and isinstance(content, str) and content.strip():
                messages.append((role, content))
        if messages:
            sessions.append((path.stem, messages))
    return sessions


def _read_skills(home: Path) -> list[Path]:
    directory = home / "skills"
    if not directory.is_dir():
        return []
    return sorted(d for d in directory.iterdir() if (d / "SKILL.md").is_file())


def cmd_hermes_import(args: argparse.Namespace) -> int:
    from .cli_cmds import _err, build_config

    home = Path(args.hermes_home).expanduser()
    if not home.is_dir():
        return _err(f"no Hermes home at {home}")

    facts = _read_memory(home)
    skill_dirs = _read_skills(home)
    sessions = _read_sessions(home)

    # The manifest — printed for BOTH dry and real runs (A5: the operator
    # chooses granularity from the counts before anything commits).
    per_class: dict[str, int] = {}
    for memory_class, _ in facts:
        per_class[memory_class] = per_class.get(memory_class, 0) + 1
    print(f"~/.hermes manifest ({home}):")
    print(f"  memory: {len(facts)} fact(s)")
    for memory_class in sorted(per_class):
        print(f"    {memory_class}: {per_class[memory_class]}")
    for memory_class, content in facts:
        first = content.splitlines()[0][:70]
        print(f"    - [{memory_class}] {first}")
    print(f"  skills: {len(skill_dirs)} candidate(s)")
    for directory in skill_dirs:
        print(f"    - {directory.name}")
    print(f"  sessions: {len(sessions)} transcript(s)")
    for title, messages in sessions:
        print(f"    - {title} ({len(messages)} messages)")

    if args.dry_run:
        print("dry run: nothing staged. Re-run without --dry-run to import.")
        return 0

    config = build_config(args.home, None)
    store = RunStore(config.db_path)
    try:
        # Memory -> proposals (pending_review). Idempotent: an existing
        # proposal from this actor with identical content is the same fact.
        existing_contents = {
            proposal.content
            for proposal in store.list_memory_proposals()
            if proposal.actor == IMPORT_ACTOR
        }
        staged = 0
        for memory_class, content in facts:
            if content in existing_contents:
                continue
            try:
                store.create_memory_proposal(
                    memory_class=memory_class,
                    content=content,
                    actor=IMPORT_ACTOR,
                    rationale=f"imported from {home}",
                )
            except MemoryError as exc:
                print(f"skipped fact: {exc}")
                continue
            staged += 1

        # Skills -> DRAFT candidates; scripts never granted (skill_md rule).
        known = {c.signature for c in store.list_candidates()}
        known_names = {c.name for c in store.list_candidates()}
        drafted = 0
        for directory in skill_dirs:
            try:
                pack = parse_skill_md(directory)
            except ValueError as exc:
                print(f"skipped skill {directory.name}: {exc}")
                continue
            template = template_from_skill_md(pack)  # allow_scripts=() always
            signature = candidate_signature(template)
            if signature in known or pack.name in known_names:
                continue
            store.add_candidate(
                SkillCandidate(
                    name=pack.name,
                    signature=signature,
                    status=DRAFT,
                    template=template,
                    source_task_ids=(),
                    occurrences=1,
                )
            )
            if pack.scripts_found:
                print(
                    f"note: {pack.name} ships scripts {list(pack.scripts_found)} — "
                    "NOT granted (grants stay a human decision at approve time)"
                )
            drafted += 1

        # Sessions -> read-only transcripts, provenance on the chat row.
        existing_titles = {
            chat.title for chat in store.list_chats() if chat.source == CHAT_SOURCE
        }
        imported_chats = 0
        for title, messages in sessions:
            if title in existing_titles:
                continue
            chat = store.create_chat(title=title, model=None, source=CHAT_SOURCE)
            for role, content in messages:
                store.add_chat_message(chat.chat_id, role=role, content=content)
            imported_chats += 1
    finally:
        store.close()

    print(
        f"staged: {staged} memory proposal(s) (pending_review), "
        f"{drafted} skill candidate(s) (draft), {imported_chats} transcript(s)"
    )
    if staged:
        print("review memory per class, in batch:")
        for memory_class in sorted(per_class):
            print(
                f"  skep memory approve-batch --actor {IMPORT_ACTOR}"
                f" --memory-class {memory_class}"
            )
        print("(individual items: skep memory proposals / approve <id> / reject <id>)")
    if drafted:
        print("skill candidates walk the normal machine: skep skill candidates / test / approve")
    print(f"{home} can be archived once the review queues are empty.")
    return 0


def register_hermes_commands(
    subcommands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    """``skep hermes import`` (v84-F8)."""
    hermes = subcommands.add_parser("hermes", help="import state out of ~/.hermes (v84)")
    hermes_sub = hermes.add_subparsers(dest="hermes_command")
    import_p = hermes_sub.add_parser(
        "import",
        help="stage Hermes memory/skills/sessions behind skep's existing gates",
    )
    import_p.add_argument(
        "--hermes-home", default="~/.hermes", help="Hermes state directory"
    )
    import_p.add_argument(
        "--dry-run", action="store_true", help="print the manifest, stage nothing"
    )
    import_p.set_defaults(func=cmd_hermes_import)
