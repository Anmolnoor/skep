"""first-run R1 (ADR 0055 D1/D3): ``skep role list|show|use``.

    skep role list              every role, the default marked
    skep role show NAME         one record and its layer as the allow/ask/deny table
    skep role use NAME          the role new chats open in
    skep role use NAME --chat ID  switch that chat's role
    skep role create NAME [--template T --provider P --model M ...]
    skep role edit NAME [flags; "" clears]  (a shipped name writes an override)
    skep role delete NAME

``use`` is the operator face of the chat's ``set_role`` card — the same
``roles.use_role`` (ADR 0050); create/edit/delete are the faces of the
create_role / edit_role / delete_role cards (H2, ADR 0064 D1).
"""

from __future__ import annotations

import argparse
import json


def cmd_role_list(args: argparse.Namespace) -> int:
    from .cli_cmds import _err, build_config
    from .roles import roles_view
    from .store import RunStore

    config = build_config(args.home, None)
    store = RunStore(config.db_path, readonly=True)
    try:
        view = roles_view(store, config.home)
    except ValueError as exc:
        return _err(str(exc))
    finally:
        store.close()
    for role in view["roles"]:
        mark = "*" if role["name"] == view["default"] else " "
        layer = role["policy_layer"] or "(none: acts as assistant)"
        castes = ", ".join(role["default_castes"])
        talks = f"{role['provider'] or 'active'}·{role['model'] or 'default'}"
        print(f"{mark} {role['name']:<12} layer: {layer:<10} talks: {talks:<20} castes: {castes}")
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


def _role_fields(args: argparse.Namespace) -> dict[str, object]:
    """The flags given, as record fields; an omitted flag keeps the field."""
    fields: dict[str, object] = {
        key: getattr(args, attr)
        for key, attr in (
            ("policy_layer", "template"),
            ("system_prompt", "prompt"),
            ("voice", "voice"),
            ("provider", "provider"),
            ("model", "model"),
        )
        if getattr(args, attr) is not None
    }
    if args.castes is not None:
        fields["default_castes"] = [c.strip() for c in args.castes.split(",") if c.strip()]
    return fields


def cmd_role_save(args: argparse.Namespace) -> int:
    from .cli_cmds import _err, build_config
    from .roles import save_role
    from .store import RunStore

    create = args.role_command == "create"
    config = build_config(args.home, None)
    store = RunStore(config.db_path)
    try:
        result = save_role(store, config.home, args.name, _role_fields(args), create=create)
    except ValueError as exc:
        return _err(str(exc), next_command="skep role list")
    finally:
        store.close()
    role = result["role"]
    talks = f"{role['provider'] or 'the active provider'} · {role['model'] or 'its default model'}"
    print(f"role {role['name']} {'created' if create else 'saved'} ({talks}): {result['path']}")
    return 0


def cmd_role_delete(args: argparse.Namespace) -> int:
    from .cli_cmds import _err, build_config
    from .roles import delete_role
    from .store import RunStore

    config = build_config(args.home, None)
    store = RunStore(config.db_path)
    try:
        result = delete_role(store, config.home, args.name)
    except ValueError as exc:
        return _err(str(exc), next_command="skep role list")
    finally:
        store.close()
    if result["reverted_to_shipped"]:
        print(f"role {args.name}: your override is gone; the shipped record applies again")
    else:
        print(f"role {args.name} deleted")
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
    for verb, text in (
        ("create", "a new role: a template, instructions, and optionally a provider + model"),
        ("edit", "change a role's fields; a shipped role gets a home override"),
    ):
        save = role_sub.add_parser(verb, help=text)
        save.add_argument("name")
        save.add_argument("--template", help="its policy template: builder, operator, assistant")
        save.add_argument("--prompt", help="instructions added to the chat's prompt")
        save.add_argument("--castes", help="comma-separated default castes")
        save.add_argument("--voice", help="style preset: concise, technical, friendly, custom:…")
        save.add_argument("--provider", help="a provider on the shelf (`skep provider list`)")
        save.add_argument("--model", help="the model its chats talk to")
        save.set_defaults(func=cmd_role_save)
    delete = role_sub.add_parser(
        "delete", help="delete a custom role (a shipped one: its override)"
    )
    delete.add_argument("name")
    delete.set_defaults(func=cmd_role_delete)
