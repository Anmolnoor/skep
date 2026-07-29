"""v87-F3: the ``skep channel status`` CLI — one honest line per channel.

    skep channel status

The 2026-07-23 field test: Discord "did not work at all" — because no
channel had ever been configured on the machine, and no surface said so.
This command reads the same store + secret files the daemon does and states
each channel's actual condition: never configured / disabled / enabled,
secret present or missing, the last delivery attempt and its result, and
(discord) the last gateway session outcome. Lazy imports mirror
memory_cmds/skill_cmds and avoid a circular import at load time.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any


def cmd_channel_status(args: argparse.Namespace) -> int:
    from .cli_cmds import build_config
    from .serve.channels import CHANNELS, resolve_channel_secret
    from .store import RunStore

    home: Path = args.home
    config = build_config(home, None)
    if not config.db_path.is_file():
        print(f"no supervisor store at {config.db_path} — run `skep serve` once first")
        return 1
    store = RunStore(config.db_path)
    try:
        configs = {c.channel: c for c in store.list_channel_configs()}
        for channel in sorted(CHANNELS):
            row = configs.get(channel)
            if row is None:
                state = (
                    "never configured (enable via PUT /api/channels/{name} or the Settings page)"
                )
            elif not row.enabled:
                state = "configured but DISABLED"
            else:
                state = f"enabled (notification_level={row.notification_level})"
            secret = (
                "present" if resolve_channel_secret(config.home, channel) is not None else "MISSING"
            )
            delivery = _breadcrumb(store.get_setting(f"channel_last_delivery:{channel}"))
            line = f"{channel:<9} {state}; secret: {secret}; last delivery: {delivery}"
            if channel == "discord":
                gateway = store.get_setting("channel_gateway_state:discord")
                line += f"; gateway: {_breadcrumb(gateway, key='state')}"
            print(line)
    finally:
        store.close()
    return 0


def _breadcrumb(value: Any, *, key: str = "note") -> str:
    if not isinstance(value, dict):
        return "never"
    ts = value.get("ts", "?")
    if key == "note":
        verdict = "ok" if value.get("ok") else "FAILED"
        return f"{value.get('note', '?')} ({verdict}) at {ts}"
    return f"{value.get(key, '?')} at {ts}"


def register_channel_commands(
    subcommands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    """``skep channel status``."""
    channel = subcommands.add_parser("channel", help="messenger channel health (v87)")
    channel_sub = channel.add_subparsers(dest="channel_command")

    status_p = channel_sub.add_parser(
        "status", help="one honest line per channel: config, secret, last delivery"
    )
    status_p.set_defaults(func=cmd_channel_status)
