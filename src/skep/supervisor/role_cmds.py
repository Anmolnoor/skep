"""first-run R1 (ADR 0055 D1/D3): ``skep role list|show|use``.

    skep role list              every role, the default marked
    skep role show NAME         one record and its layer as the allow/ask/deny table
    skep role use NAME          the role new chats open in
    skep role use NAME --chat ID  switch that chat's role

``use`` is the operator face of the chat's ``set_role`` card — the same
``roles.use_role`` (ADR 0050).
"""

from __future__ import annotations

import argparse
import json


def cmd_role_list(args: argparse.Namespace) -> int:
    from .cli_cmds import _err, build_config
    from .roles import roles_view
    from .store import RunStore

    config = build_config(args.home, None)
    store = RunStore(config.db_path)
    try:
        view = roles_view(store, config.home)
    except ValueError as exc:
        return _err(str(exc))
    finally:
        store.close()
    for role in view["roles"]:
        mark = "*" if role["name"] == view["default"] else " "
        layer = role["policy_layer"] or "(no layer)"
        castes = ", ".join(role["default_castes"])
        print(f"{mark} {role['name']:<12} layer: {layer:<10} castes: {castes}")
    return 0


def cmd_role_show(args: argparse.Namespace) -> int:
    from .cli_cmds import _err, _print_table, build_config
    from .policy_templates import explain_view
    from .roles import resolve_role

    config = build_config(args.home, None)
    try:
        role = resolve_role(args.name, config.home)
        view = explain_view(f"role:{role.name}", config.home)
    except ValueError as exc:
        return _err(str(exc))
    print(json.dumps(role.view(), indent=2, sort_keys=True))
    print()
    _print_table(view["table"])
    return 0


def cmd_role_use(args: argparse.Namespace) -> int:
    from .cli_cmds import _err, build_config
    from .roles import use_role
    from .store import RunStore

    config = build_config(args.home, None)
    store = RunStore(config.db_path)
    try:
        result = use_role(store, config.home, args.name, chat_id=args.chat)
    except ValueError as exc:
        return _err(str(exc), next_command="skep role list")
    finally:
        store.close()
    if args.chat:
        print(f"chat {args.chat} now runs in {result['layer']}")
    else:
        print(f"new chats open in role {result['default_role']}")
    return 0


def register_role_commands(
    subcommands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    from .cli_cmds import _group_usage  # local: cli_cmds registers this module

    role = subcommands.add_parser(
        "role", help="roles: a prompt, a voice and a policy layer under the baseline (ADR 0055)"
    )
    role.set_defaults(func=_group_usage(role))
    role_sub = role.add_subparsers(dest="role_command")
    listing = role_sub.add_parser("list", help="every role; * marks the one new chats open in")
    listing.set_defaults(func=cmd_role_list)
    show = role_sub.add_parser("show", help="one role's record and its layer as a table")
    show.add_argument("name")
    show.set_defaults(func=cmd_role_show)
    use = role_sub.add_parser("use", help="the role new chats open in, or one chat's role")
    use.add_argument("name")
    use.add_argument("--chat", default=None, help="switch this chat instead of the default")
    use.set_defaults(func=cmd_role_use)
