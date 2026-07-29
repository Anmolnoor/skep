"""v13 Step 5: the ``skep memory`` CLI — curated-memory review and search.

    skep memory list [--project P]
    skep memory search "query" [--project P]
    skep memory show <memory_id>
    skep memory forget <memory_id>
    skep memory proposals [--state pending_review]
    skep memory propose --from-note <id> | --from-task <id> [--class C] [--project P]
    skep memory approve <proposal_id>
    skep memory reject <proposal_id> --reason "not durable"
    skep memory approve-batch [--actor A] [--memory-class C]   (v84-A5)
    skep memory reject-batch [--actor A] [--memory-class C] --reason R

Every mutation reuses the same governed store methods as the API and chat — no
shadow path to durable memory. Lazy imports of ``build_config``/``_err`` mirror
skill_cmds/serve_cmds and avoid a circular import at load time.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from ..workers.curator import classify_memory_class
from .memory import MemoryError, MemoryProposal, MemorySource
from .store import RunStore

CLI_ACTOR = "cli-user"


def _open_store_or_empty(home: Path) -> RunStore | None:
    from .cli_cmds import build_config

    config = build_config(home, None)
    if not config.db_path.is_file():
        return None
    return RunStore(config.db_path)


def cmd_memory_list(args: argparse.Namespace) -> int:
    store = _open_store_or_empty(args.home)
    if store is None:
        print("no memory yet")
        return 0
    try:
        items = store.list_memory_items(project_id=args.project)
    finally:
        store.close()
    if not items:
        print("no memory yet")
        return 0
    for item in items:
        scope = item.project_id or "global"
        print(f"{item.memory_id}  [{item.memory_class}] ({scope})  {item.content}")
    return 0


def cmd_memory_search(args: argparse.Namespace) -> int:
    store = _open_store_or_empty(args.home)
    if store is None:
        print("no matches")
        return 0
    try:
        hits = store.search_memory(args.query, project_id=args.project)
    finally:
        store.close()
    if not hits:
        print("no matches")
        return 0
    for item in hits:
        print(f"{item.memory_id}  [{item.memory_class}]  {item.content}")
    return 0


def cmd_memory_show(args: argparse.Namespace) -> int:
    from .cli_cmds import _err

    store = _open_store_or_empty(args.home)
    if store is None:
        return _err(f"no memory item {args.memory_id!r}.")
    try:
        item = store.get_memory_item(args.memory_id)
    finally:
        store.close()
    if item is None:
        return _err(f"no memory item {args.memory_id!r}.")
    print(f"id:      {item.memory_id}")
    print(f"class:   {item.memory_class}")
    print(f"scope:   {item.project_id or 'global'}")
    print(f"active:  {item.active}")
    print(f"created: {item.created_at}")
    if item.proposal_id is not None:
        print(f"from:    proposal {item.proposal_id}")
    print(f"\n{item.content}")
    return 0


def cmd_memory_forget(args: argparse.Namespace) -> int:
    from .cli_cmds import _err

    store = _open_store_or_empty(args.home)
    if store is None:
        return _err(f"no memory item {args.memory_id!r}.")
    try:
        forgotten = store.forget_memory_item(args.memory_id, actor=CLI_ACTOR)
    finally:
        store.close()
    if not forgotten:
        return _err(f"no active memory item {args.memory_id!r}.")
    print(f"forgot memory {args.memory_id!r}")
    return 0


def cmd_memory_proposals(args: argparse.Namespace) -> int:
    store = _open_store_or_empty(args.home)
    if store is None:
        print("no proposals")
        return 0
    try:
        proposals = store.list_memory_proposals(state=args.state)
    finally:
        store.close()
    if not proposals:
        print("no proposals")
        return 0
    for p in proposals:
        print(f"{p.proposal_id}  [{p.memory_class}] {p.state}  {p.content}")
    return 0


def cmd_memory_propose(args: argparse.Namespace) -> int:
    from .cli_cmds import _err

    if bool(args.from_note) == bool(args.from_task):
        return _err("propose requires exactly one of --from-note or --from-task.")
    store = _open_store_or_empty(args.home)
    if store is None:
        return _err("no store yet — add a note or task first.")
    try:
        if args.from_note:
            note = store.get_note(args.from_note)
            if note is None:
                return _err(f"no note {args.from_note!r}.")
            content, source = note.content, MemorySource(kind="note", source_id=args.from_note)
        else:
            task = store.get_task(args.from_task)
            if task is None:
                return _err(f"no task {args.from_task!r}.")
            content, source = task.title, MemorySource(kind="task", source_id=args.from_task)
        memory_class = args.memory_class or classify_memory_class(
            content, has_project=args.project is not None
        )
        try:
            proposal = store.create_memory_proposal(
                memory_class=memory_class,
                content=content,
                actor=CLI_ACTOR,
                rationale=args.rationale,
                project_id=args.project,
                sources=(source,),
            )
        except MemoryError as exc:
            return _err(str(exc))
    finally:
        store.close()
    print(f"proposed {proposal.proposal_id} [{proposal.memory_class}] ({proposal.state})")
    return 0


def cmd_memory_approve(args: argparse.Namespace) -> int:
    from .cli_cmds import _err

    store = _open_store_or_empty(args.home)
    if store is None:
        return _err(f"no proposal {args.proposal_id!r}.")
    try:
        try:
            item = store.approve_memory_proposal(args.proposal_id, actor=CLI_ACTOR)
        except MemoryError as exc:
            return _err(str(exc))
    finally:
        store.close()
    print(f"approved — durable memory {item.memory_id} created")
    return 0


def cmd_memory_reject(args: argparse.Namespace) -> int:
    from .cli_cmds import _err

    store = _open_store_or_empty(args.home)
    if store is None:
        return _err(f"no proposal {args.proposal_id!r}.")
    try:
        try:
            store.reject_memory_proposal(args.proposal_id, actor=CLI_ACTOR, reason=args.reason)
        except MemoryError as exc:
            return _err(str(exc))
    finally:
        store.close()
    print(f"rejected {args.proposal_id!r}: {args.reason}")
    return 0


def _batch_targets(
    store: RunStore, *, actor: str, memory_class: str | None
) -> list[MemoryProposal]:
    proposals = [
        proposal
        for proposal in store.list_memory_proposals(state="pending_review")
        if proposal.actor == actor
    ]
    if memory_class is not None:
        proposals = [p for p in proposals if p.memory_class == memory_class]
    return proposals


def cmd_memory_approve_batch(args: argparse.Namespace) -> int:
    """v84-A5: batch review for imports — one decision per actor/class, every
    item listed before it lands, each still individually rejectable first."""
    from .cli_cmds import _err

    store = _open_store_or_empty(args.home)
    if store is None:
        return _err("no proposals yet.")
    try:
        targets = _batch_targets(store, actor=args.actor, memory_class=args.memory_class)
        if not targets:
            print("nothing pending for that actor/class")
            return 0
        approved = 0
        for proposal in targets:
            first_line = proposal.content.splitlines()[0][:70]
            print(f"  {proposal.proposal_id} [{proposal.memory_class}] {first_line}")
            try:
                store.approve_memory_proposal(proposal.proposal_id, actor=CLI_ACTOR)
            except MemoryError as exc:
                print(f"  skipped {proposal.proposal_id}: {exc}")
                continue
            approved += 1
    finally:
        store.close()
    print(f"approved {approved} proposal(s)")
    return 0


def cmd_memory_reject_batch(args: argparse.Namespace) -> int:
    from .cli_cmds import _err

    store = _open_store_or_empty(args.home)
    if store is None:
        return _err("no proposals yet.")
    try:
        targets = _batch_targets(store, actor=args.actor, memory_class=args.memory_class)
        if not targets:
            print("nothing pending for that actor/class")
            return 0
        for proposal in targets:
            store.reject_memory_proposal(proposal.proposal_id, actor=CLI_ACTOR, reason=args.reason)
            print(f"  rejected {proposal.proposal_id}")
    finally:
        store.close()
    print(f"rejected {len(targets)} proposal(s): {args.reason}")
    return 0


def register_memory_commands(
    subcommands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    """``skep memory list|search|show|forget|proposals|propose|approve|reject``."""
    memory = subcommands.add_parser("memory", help="curated durable memory (v13)")
    memory_sub = memory.add_subparsers(dest="memory_command")

    list_p = memory_sub.add_parser("list", help="list durable memory items")
    list_p.add_argument("--project", default=None, help="scope to a project (plus global)")
    list_p.set_defaults(func=cmd_memory_list)

    search_p = memory_sub.add_parser("search", help="full-text search durable memory")
    search_p.add_argument("query")
    search_p.add_argument("--project", default=None, help="scope to a project (plus global)")
    search_p.set_defaults(func=cmd_memory_search)

    show_p = memory_sub.add_parser("show", help="show one memory item")
    show_p.add_argument("memory_id")
    show_p.set_defaults(func=cmd_memory_show)

    forget_p = memory_sub.add_parser("forget", help="forget (soft-delete) a memory item")
    forget_p.add_argument("memory_id")
    forget_p.set_defaults(func=cmd_memory_forget)

    proposals_p = memory_sub.add_parser("proposals", help="list memory proposals")
    proposals_p.add_argument("--state", default=None, help="filter by state (e.g. pending_review)")
    proposals_p.set_defaults(func=cmd_memory_proposals)

    propose_p = memory_sub.add_parser("propose", help="propose memory from a note or task")
    propose_p.add_argument("--from-note", dest="from_note", default=None)
    propose_p.add_argument("--from-task", dest="from_task", default=None)
    propose_p.add_argument(
        "--class",
        dest="memory_class",
        default=None,
        help="memory class (auto-classified if omitted)",
    )
    propose_p.add_argument("--project", default=None)
    propose_p.add_argument("--rationale", default=None)
    propose_p.set_defaults(func=cmd_memory_propose)

    approve_p = memory_sub.add_parser("approve", help="approve a proposal into durable memory")
    approve_p.add_argument("proposal_id")
    approve_p.set_defaults(func=cmd_memory_approve)

    reject_p = memory_sub.add_parser("reject", help="reject a proposal with a reason")
    reject_p.add_argument("proposal_id")
    reject_p.add_argument("--reason", required=True, help="why it is not durable")
    reject_p.set_defaults(func=cmd_memory_reject)

    # v84-A5: batch review, scoped to one actor (default: the hermes import)
    # and optionally one class — a 400-item import queue is reviewed per
    # class, never rubber-stamped one item at a time.
    approve_batch_p = memory_sub.add_parser(
        "approve-batch", help="approve all pending proposals from one actor/class (v84)"
    )
    approve_batch_p.add_argument("--actor", default="hermes-import")
    approve_batch_p.add_argument("--memory-class", dest="memory_class", default=None)
    approve_batch_p.set_defaults(func=cmd_memory_approve_batch)

    reject_batch_p = memory_sub.add_parser(
        "reject-batch", help="reject all pending proposals from one actor/class"
    )
    reject_batch_p.add_argument("--actor", default="hermes-import")
    reject_batch_p.add_argument("--memory-class", dest="memory_class", default=None)
    reject_batch_p.add_argument("--reason", required=True)
    reject_batch_p.set_defaults(func=cmd_memory_reject_batch)
