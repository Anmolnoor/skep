"""ADR 0054 (B1-3): the ``skep acp`` CLI — the process an ACP client spawns.

    skep acp govern [--repo PATH] [--actor NAME] -- <agent argv…>
    skep acp doctor -- <agent argv…>
    skep acp install-hint

Lazy imports mirror channel_cmds and keep the proxy off the CLI's load path.
"""

from __future__ import annotations

import argparse


def _agent_argv(args: argparse.Namespace) -> list[str]:
    argv = list(args.agent)
    if argv and argv[0] == "--":
        argv = argv[1:]
    if not argv:
        args.acp_parser.error("no agent command given (usage: ... -- <agent argv…>)")
    return argv


def cmd_acp_govern(args: argparse.Namespace) -> int:
    from .acp.govern import run

    return run(_agent_argv(args), home=args.home, repo=args.repo, actor=args.actor)


def cmd_acp_doctor(args: argparse.Namespace) -> int:
    from .acp.govern import doctor

    return doctor(_agent_argv(args), home=args.home)


def cmd_acp_install_hint(args: argparse.Namespace) -> int:
    from .acp.govern import install_hint

    return install_hint(args.home)


def register_acp_commands(
    subcommands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    """``skep acp govern|doctor|install-hint``."""
    from .cli_cmds import _group_usage  # local: cli_cmds registers this module

    acp = subcommands.add_parser("acp", help="govern an ACP agent for another client (ADR 0054)")
    acp.set_defaults(func=_group_usage(acp))
    acp_sub = acp.add_subparsers(dest="acp_command")

    govern = acp_sub.add_parser(
        "govern", help="relay an ACP agent's stdio, answering its permission asks from policy"
    )
    govern.add_argument("--repo", help="decide against this checkout, not session/new's cwd")
    govern.add_argument("--actor", help="ledger actor for decisions (default acp:<agent>)")
    govern.add_argument("agent", nargs=argparse.REMAINDER, help="-- the agent's argv")
    govern.set_defaults(func=cmd_acp_govern, acp_parser=govern)

    doctor = acp_sub.add_parser(
        "doctor", help="initialize the agent and a dry session/new; print which rings are live"
    )
    doctor.add_argument("agent", nargs=argparse.REMAINDER, help="-- the agent's argv")
    doctor.set_defaults(func=cmd_acp_doctor, acp_parser=doctor)

    hint = acp_sub.add_parser(
        "install-hint", help="print the rows to paste into T3 Code's Local ACP command dialog"
    )
    hint.set_defaults(func=cmd_acp_install_hint)
