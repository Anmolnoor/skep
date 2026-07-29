from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from . import __version__
from .profile import run_personal_setup
from .server import serve
from .status import build_status, format_doctor_report, status_json
from .supervisor.cli_cmds import cmd_status_personal, register_supervisor_commands
from .worker_contract import CONTRACT_VERSION


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.version:
        print(f"skep {__version__} (worker contract {CONTRACT_VERSION})")
        return 0
    if not hasattr(args, "func"):
        parser.print_help()
        return 2
    return int(args.func(args))


def default_home() -> Path:
    return Path(os.environ.get("SKEP_HOME", Path.home() / ".skep"))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="skep")
    parser.add_argument(
        "--home",
        type=Path,
        default=default_home(),
        help="supervisor home directory (default: $SKEP_HOME or ~/.skep)",
    )
    parser.add_argument(
        "--version", action="store_true", help="show Skep and worker contract versions"
    )
    subcommands = parser.add_subparsers(dest="command")

    setup = subcommands.add_parser(
        "setup",
        help="create or update the local personal profile",
    )
    setup.add_argument("--personal", action="store_true", help="initialize personal-mode storage")
    setup.add_argument(
        "--provider",
        default=os.environ.get("SKEP_PROVIDER", "unconfigured"),
        help="provider name to record in the personal profile",
    )
    setup.add_argument(
        "--model",
        default=os.environ.get("SKEP_MODEL", ""),
        help="model name to record in the personal profile",
    )
    setup.add_argument(
        "--endpoint",
        default=os.environ.get("SKEP_PROVIDER_ENDPOINT"),
        help="provider API endpoint URL",
    )
    setup.add_argument(
        "--api-key-env",
        default=os.environ.get("SKEP_PROVIDER_API_KEY_ENV"),
        help="environment variable that holds the provider API key",
    )
    # v40-F12 (v36-F8): pick a policy template — preview, apply, switch diffs.
    setup.add_argument(
        "--template",
        default=None,
        metavar="NAME",
        help="apply a policy template (locked-down, personal-dev, homelab-ops, assistant)",
    )
    setup.add_argument(
        "--dry-run",
        action="store_true",
        help="with --template: print the resolved policy table and write nothing",
    )
    setup.add_argument(
        "--apply",
        action="store_true",
        help="with --template: confirm switching an existing policy document",
    )
    setup.set_defaults(func=_setup)

    doctor = subcommands.add_parser(
        "doctor",
        help="check local configuration and runtime readiness",
    )
    doctor.set_defaults(func=_doctor)

    status = subcommands.add_parser(
        "status",
        help="show setup, provider, sandbox, and approval status",
    )
    status.add_argument("--json", action="store_true", help="print machine-readable JSON")
    status.add_argument(
        "--personal",
        action="store_true",
        help="show recent supervised runs from the personal store",
    )
    status.set_defaults(func=_status)

    start = subcommands.add_parser("start", help="start the local status dashboard")
    start.add_argument("--host", default="127.0.0.1", help="dashboard bind host")
    start.add_argument("--port", type=int, default=8765, help="dashboard bind port")
    start.set_defaults(func=_start)

    # v38: the terminal face — a REPL client of the serve daemon (never the store).
    chat = subcommands.add_parser(
        "chat", help="talk to the Queen in the terminal (needs `skep serve`)"
    )
    chat.add_argument(
        "--url",
        default=None,
        help="serve base URL (default: $SKEP_SERVE_URL or http://127.0.0.1:8765)",
    )
    chat.add_argument("--chat", dest="chat_id", default=None, help="resume this chat id")
    chat.add_argument(
        "--continue",
        dest="continue_latest",
        action="store_true",
        help="resume the most recent chat",
    )
    chat.add_argument("--thinking", action="store_true", help="show the model's thinking, dim")
    chat.add_argument(
        "--oneshot",
        metavar="MESSAGE",
        default=None,
        help="send one message to a new chat, stream the reply, exit (no prompts)",
    )
    chat.add_argument(
        "--yes",
        action="store_true",
        help="with --oneshot: pre-confirm every card this turn proposes "
        "(explicit consent for THIS invocation; default stays skip-and-report)",
    )
    chat.set_defaults(func=_chat)

    register_supervisor_commands(subcommands)
    return parser


def _provider_flags_unset(args: argparse.Namespace) -> bool:
    return (
        args.provider == "unconfigured"
        and not args.model
        and args.endpoint is None
        and args.api_key_env is None
    )


def _setup_wizard(args: argparse.Namespace) -> None:
    """v37-F3: TTY-only prompts in front of the existing setup primitives.

    Flags and SKEP_* env vars always win (no prompts), and a non-TTY stdin is
    byte-identical to the flag-driven path. The wizard asks for the API-key
    env var NAME, never the key itself (the v19-F9 pattern).
    """
    print("no provider flags given — answer a few prompts (Enter keeps the default)")
    provider = input("provider (ollama / openai-compatible / mock) [ollama]: ").strip() or "ollama"
    args.provider = provider
    args.model = input("model (e.g. qwen3:14b) []: ").strip()
    default_endpoint = "http://localhost:11434" if provider == "ollama" else ""
    endpoint = input(f"endpoint [{default_endpoint or 'none'}]: ").strip() or default_endpoint
    args.endpoint = endpoint or None
    key_env = input("API key env var NAME (never the key itself) [none]: ").strip()
    args.api_key_env = key_env or None


def _setup(args: argparse.Namespace) -> int:
    if args.template:
        return _setup_template(args)
    if not args.personal:
        print("V1 only supports personal setup. Re-run with: setup --personal", file=sys.stderr)
        return 2

    if _provider_flags_unset(args) and sys.stdin.isatty():
        _setup_wizard(args)

    try:
        result = run_personal_setup(
            args.home,
            provider=args.provider,
            model=args.model,
            endpoint=args.endpoint,
            api_key_env=args.api_key_env,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    action = "created" if result.created else "updated" if result.updated else "loaded"
    print(f"Personal profile {action}: {args.home}")
    print(format_doctor_report(build_status(args.home)))
    # v27-F5: setup ends by saying what comes next, not just what happened.
    print("next steps:")
    print("  skep serve     # the daemon + web UI; the access token prints in the boot log")
    print("  open http://127.0.0.1:8765/ and finish setup in the browser")
    print("  skep chat      # talk to the Queen right here in the terminal")
    print("  skep doctor    # re-check readiness any time")
    return 0


def _setup_template(args: argparse.Namespace) -> int:
    """v40-F12 (v36-F8): pick -> preview -> apply; switching later diffs first."""
    from .supervisor.cli_cmds import build_config
    from .supervisor.policy_schema import (
        POLICY_DOCUMENT_SETTINGS_KEY,
        document_from_settings,
    )
    from .supervisor.policy_templates import (
        derived_global_knobs,
        diff_resolved_views,
        load_policy_template,
        render_policy_table,
        resolved_view,
    )
    from .supervisor.store import RunStore

    try:
        document = load_policy_template(args.template)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    view = resolved_view(document)
    print(f"policy template: {document.template}"
          + (f"  (pack: {document.pack})" if document.pack else ""))
    print(render_policy_table(view))
    if args.dry_run:
        print("dry run — nothing written")
        return 0

    config = build_config(args.home, None)
    store = RunStore(config.db_path)
    try:
        existing = document_from_settings(store.get_setting(POLICY_DOCUMENT_SETTINGS_KEY))
        if existing is not None and existing.template != document.template:
            diff = diff_resolved_views(resolved_view(existing), view)
            if diff and not args.apply:
                print(f"switching {existing.template} -> {document.template} would change:")
                for line in diff:
                    print(f"  {line}")
                print("re-run with --apply to switch", file=sys.stderr)
                return 1
        store.set_setting(POLICY_DOCUMENT_SETTINGS_KEY, document.model_dump_json())
        # The legacy knobs the compiled run policy reads (F7) follow the
        # template; machine-specific knobs (trusted roots, execution mode)
        # stay the operator's.
        from .supervisor.serve.actions import update_policy
        from .supervisor.serve.settings import ConfigHolder

        update_policy(store, ConfigHolder(config, store), derived_global_knobs(document))
    finally:
        store.close()
    print(f"applied template {document.template!r}")
    print("next steps:")
    print("  skep serve     # the daemon + web UI")
    print("  skep chat      # talk to the Queen right here in the terminal")
    print(f"  skep setup --template {document.template} --dry-run   # review any time")
    return 0


def _doctor(args: argparse.Namespace) -> int:
    status = build_status(args.home)
    print(format_doctor_report(status), end="")
    return 0 if status["overall"] == "ready" else 1


def _status(args: argparse.Namespace) -> int:
    if args.personal:
        return cmd_status_personal(args)
    status = build_status(args.home)
    if args.json:
        print(status_json(status), end="")
    else:
        print(format_doctor_report(status), end="")
    return 0 if status["overall"] == "ready" else 1


def _chat(args: argparse.Namespace) -> int:
    # Lazy: httpx (and nothing heavier) loads only when the REPL actually runs.
    from .cli_chat import cmd_chat

    return cmd_chat(args)


def _start(args: argparse.Namespace) -> int:
    url, server = serve(args.home, args.host, args.port)
    print(f"Skep dashboard: {url}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped Skep.")
    finally:
        server.server_close()
    return 0
