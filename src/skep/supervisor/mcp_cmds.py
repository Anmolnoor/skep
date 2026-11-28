"""v135 (ADR 0069 2.5): ``skep mcp secret set|list|unset``.

    skep mcp secret set SERVER_ID NAME     read the value from a prompt (or stdin)
    skep mcp secret list SERVER_ID         the names set, and which the server lists
    skep mcp secret unset SERVER_ID NAME   remove one

A strict MCP server gets nothing from serve's environment; the secrets it
lists in ``secret_names`` come from its own file under the skep home, which
only this command writes. The value never passes through an argument, a
chat or a card.
"""

from __future__ import annotations

import argparse
import sys


def _home(args: argparse.Namespace) -> object:
    from .cli_cmds import build_config

    return build_config(args.home, None).home.parent


def cmd_mcp_secret_set(args: argparse.Namespace) -> int:
    import getpass

    from .cli_cmds import _err
    from .mcp_client import MCPError
    from .mcp_sandbox import set_secret

    if sys.stdin.isatty():
        value = getpass.getpass(f"value for {args.name} (not echoed): ")
    else:
        value = sys.stdin.read().rstrip("\r\n")
    try:
        path = set_secret(_home(args), args.server_id, args.name, value)  # type: ignore[arg-type]
    except MCPError as exc:
        return _err(str(exc))
    print(f"set {args.name} for {args.server_id} ({path}, mode 0600)")
    return 0


def cmd_mcp_secret_list(args: argparse.Namespace) -> int:
    from .cli_cmds import _err, build_config
    from .mcp_client import MCPError, load_mcp_servers
    from .mcp_sandbox import load_secrets
    from .store import RunStore

    config = build_config(args.home, None)
    try:
        held = load_secrets(config.home.parent, args.server_id)
    except MCPError as exc:
        return _err(str(exc))
    store = RunStore(config.db_path, readonly=True)
    try:
        server = load_mcp_servers(store).get(args.server_id)
    finally:
        store.close()
    listed = server.secret_names if server is not None else ()
    for name in sorted({*held, *listed}):
        state = "set" if held.get(name) else "NOT SET"
        use = "listed by the server" if name in listed else "not listed by the server"
        print(f"{name}: {state} ({use})")
    if not held and not listed:
        print(f"no secrets for {args.server_id}")
    return 0


def cmd_mcp_secret_unset(args: argparse.Namespace) -> int:
    from .cli_cmds import _err
    from .mcp_client import MCPError
    from .mcp_sandbox import unset_secret

    try:
        removed = unset_secret(_home(args), args.server_id, args.name)  # type: ignore[arg-type]
    except MCPError as exc:
        return _err(str(exc))
    print(f"{'unset' if removed else 'was not set:'} {args.name} for {args.server_id}")
    return 0


def register_mcp_commands(
    subcommands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    from .cli_cmds import _group_usage  # local: cli_cmds registers this module

    mcp = subcommands.add_parser("mcp", help="MCP servers: per-server secrets (ADR 0069)")
    mcp.set_defaults(func=_group_usage(mcp))
    mcp_sub = mcp.add_subparsers(dest="mcp_command")
    secret = mcp_sub.add_parser("secret", help="a strict server's own secrets file")
    secret.set_defaults(func=_group_usage(secret))
    secret_sub = secret.add_subparsers(dest="secret_command")
    set_ = secret_sub.add_parser("set", help="store NAME's value, read from a prompt or stdin")
    set_.add_argument("server_id")
    set_.add_argument("name")
    set_.set_defaults(func=cmd_mcp_secret_set)
    listing = secret_sub.add_parser("list", help="the names set, never the values")
    listing.add_argument("server_id")
    listing.set_defaults(func=cmd_mcp_secret_list)
    unset = secret_sub.add_parser("unset", help="remove NAME's value")
    unset.add_argument("server_id")
    unset.add_argument("name")
    unset.set_defaults(func=cmd_mcp_secret_unset)
