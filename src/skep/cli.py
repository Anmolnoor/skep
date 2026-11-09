from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

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
        help="apply a policy template (builder, operator, assistant)",
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

    # first-run O3 (ADR 0056 D7): the guided form of setup, five resumable steps.
    onboard = subcommands.add_parser(
        "onboard",
        help="guided first run: model, work directory, sandbox, policy baseline, role",
    )
    onboard.add_argument(
        "--provider", default=None, help="mock, ollama, openai-compatible, or a preset id"
    )
    onboard.add_argument("--model", default=None, help="model name")
    onboard.add_argument("--endpoint", default=None, help="provider API endpoint URL")
    onboard.add_argument(
        "--api-key-env",
        default=None,
        help="environment variable that holds the provider API key (its NAME, never the key)",
    )
    onboard.add_argument("--workdir", default=None, help="work directory (created if missing)")
    onboard.add_argument("--sandbox", default=None, help="auto, podman or none")
    onboard.add_argument(
        "--template", default=None, help="builder, operator or assistant (default: assistant)"
    )
    onboard.add_argument(
        "--role", default=None, help="builder, operator or assistant (default: the template)"
    )
    onboard.add_argument(
        "--yes", action="store_true", help="take the defaults and skip the typed `yes`"
    )
    onboard.add_argument("--no-chat", action="store_true", help="stop before opening skep chat")
    onboard.add_argument(
        "--check",
        action="store_true",
        help="doctor plus the baseline, engine, sandbox, workdir and egress checks (read-only)",
    )
    onboard.set_defaults(func=_onboard)

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
        "--role", default=None, help="open the new chat in this role (default: skep role list's *)"
    )
    chat.add_argument(
        "--oneshot",
        metavar="MESSAGE",
        default=None,
        help="send one message to a new chat, stream the reply, exit (no prompts)",
    )
    chat.add_argument(
        "--yes",
        action="store_true",
        help="with --oneshot: pre-confirm every card this turn proposes, once — "
        "never a policy card (allowlist/grant changes stay pending, named for "
        "/approve); explicit consent for THIS invocation, default stays skip-and-report",
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
        deprecation_line,
        diff_resolved_views,
        explain_view,
        load_policy_template,
        resolved_view,
    )
    from .supervisor.store import RunStore

    try:
        template = load_policy_template(args.template)
        explained = explain_view(args.template)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if explained["deprecated_alias"]:
        print(deprecation_line(explained["deprecated_alias"]), file=sys.stderr)
    document = template.document()
    view = resolved_view(document)
    print(explained["table"])
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
        # The template's knobs (O1: stated in the file, no longer derived from
        # its rules) are written beside the document so the compiled run
        # policy (F7) reflects the template too; machine-specific knobs
        # (trusted roots, execution mode) stay the operator's.
        from .supervisor.serve.actions import update_policy
        from .supervisor.serve.settings import ConfigHolder

        update_policy(store, ConfigHolder(config, store), dict(template.settings))
    finally:
        store.close()
    print(f"applied template {document.template!r}")
    print("next steps:")
    print("  skep serve     # the daemon + web UI")
    print("  skep chat      # talk to the Queen right here in the terminal")
    print(f"  skep setup --template {document.template} --dry-run   # review any time")
    return 0


def _onboard(args: argparse.Namespace) -> int:
    """first-run O3: `skep onboard` — resume at the first incomplete step;
    a flag that differs from a step's record re-runs that step."""
    from fastapi import HTTPException

    from .supervisor import onboard
    from .supervisor.cli_cmds import build_config
    from .supervisor.serve.settings import ConfigHolder
    from .supervisor.store import RunStore

    config = build_config(args.home, None)
    if args.check:
        result = onboard.onboard_check(config.home)
        print(result["report"], end="")
        return 0 if result["ready"] else 1
    store = RunStore(config.db_path)
    try:
        code = _onboard_steps(args, ConfigHolder(config, store), store)
    except HTTPException as exc:
        print(f"error: {exc.detail}", file=sys.stderr)
        code = 1
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        code = 1
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        code = 130
    finally:
        store.close()
    if code:
        print("finished steps are kept; run skep onboard again to resume", file=sys.stderr)
        return code
    print("onboarded — check any time: skep onboard --check")
    if args.no_chat:
        return 0
    role = onboard.onboard_status(config.home)["steps"]["role"]["role"]
    return _onboard_chat(config.home, role)


def _onboard_steps(args: argparse.Namespace, holder: Any, store: Any) -> int:
    import getpass

    from .supervisor import onboard
    from .supervisor.policy_templates import TEMPLATE_NAMES, explain_view
    from .supervisor.serve.setup import default_workspace_path

    home = holder.current.home.parent
    records = onboard.onboard_status(holder.current.home)["steps"]

    def ask(prompt: str, default: str) -> str:
        if args.yes:
            return default
        try:
            return input(f"{prompt} [{default}]: ").strip() or default
        except EOFError:
            return default

    # ① model
    given = {
        key: value
        for key, value in (
            ("provider", args.provider),
            ("model", args.model),
            ("endpoint", args.endpoint),
            ("api_key_env", args.api_key_env),
        )
        if value is not None
    }
    record = records["model"]
    if record and all(record.get(key) == value for key, value in given.items()):
        print(f"① model: done — {record['provider']} {record['model']}")
    else:
        print("① model")
        if not given:
            found = onboard.detect_ollama()
            if found is not None:
                print(
                    f"  local Ollama at {onboard.OLLAMA_LOCAL}: {', '.join(found) or 'no models'}"
                )
            if args.yes:
                if not found:
                    raise ValueError("no local Ollama with a model — pass --provider and --model")
                given = {"provider": "ollama", "model": found[0]}
            else:
                answers = argparse.Namespace()
                _setup_wizard(answers)
                given = {key: value for key, value in vars(answers).items() if value}
                if not given.get("model") and found:
                    given["model"] = found[0]
        record = onboard.onboard_model(
            holder,
            store,
            provider=given.get("provider", "ollama"),
            model=given.get("model", ""),
            endpoint=given.get("endpoint"),
            api_key_env=given.get("api_key_env"),
        )
        print(f"  test: {record['test']}")
        print(f"  egress: {record['egress']}")

    # ② work directory
    record = records["workdir"]
    wanted = Path(args.workdir).expanduser().resolve() if args.workdir else None
    if record and wanted in (None, Path(record["path"])):
        print(f"② work directory: done — {record['path']}")
    else:
        print("② work directory")
        path = args.workdir or ask("  work directory", str(default_workspace_path()))
        record = onboard.onboard_workdir(holder, store, path=path)
        print(f"  {'created' if record['created'] else 'using'} {record['path']}")
        print(f"  registered as project {record['project_id']}")
        if record["verify_command"]:
            print(f"  verify_command (inferred, pinned): {record['verify_command']}")
        else:
            print("  verify_command: none inferred (no entry point) — re-verify falls back")
            print("  to the worker's own command until one is pinned")
        if not record["git"]:
            print("  not a git repository — dispatch needs one (`git init` it, or /workon)")

    # ③ sandbox
    record = records["sandbox"]
    if record and args.sandbox in (None, record["backend"]):
        print(f"③ sandbox: done — {record['backend']}")
    else:
        print("③ sandbox")
        backend = args.sandbox
        if backend is None:
            detected = onboard.detect_sandboxes()
            for name, view in detected["backends"].items():
                state = "usable" if view["usable"] else "unusable"
                print(f"  {name}: {state} — {view['detail']}")
            print(f"  docker: {detected['docker']['detail']}")
            backend = ask("  sandbox backend (auto / podman / none)", detected["recommended"])
        record = onboard.onboard_sandbox(holder, store, backend=backend)
        print(f"  {backend}: {record['rings']}")

    # ④ template
    record = records["template"]
    if record and args.template in (None, record["template"]):
        print(f"④ template: done — {record['template']} locked")
    else:
        print("④ template")
        template = args.template
        shown = False
        if template is None:
            if not args.yes:
                for name in TEMPLATE_NAMES:
                    print(explain_view(name)["table"])
                    print()
                shown = True
            template = ask("  template (builder / operator / assistant)", onboard.DEFAULT_TEMPLATE)
        preview = onboard.template_preview(holder, template=template)
        if not preview["locked"]:
            if not shown or preview["tightened"]:
                print(preview["table"])
            for heading, lines in (
                ("no sandbox ring — the template is tightened:", preview["tightened"]),
                ("changes against the locked baseline:", preview["diff"]),
            ):
                if lines:
                    print(f"  {heading}")
                    print("\n".join(f"    {line}" for line in lines))
            if not args.yes:
                try:
                    answer = input("type yes to lock this as the policy baseline: ")
                except EOFError:
                    answer = ""
                if answer.strip() != "yes":
                    print("not approved — nothing was locked", file=sys.stderr)
                    return 1
        record = onboard.onboard_template(
            holder,
            template=template,
            approved_by=getpass.getuser(),
            expect_sha256=preview["sha256"],
        )
        print(f"  baseline locked: {record['template']} sha256 {record['sha256'][:12]}")

    # ⑤ role
    record = records["role"]
    if record and args.role in (None, record["role"]):
        print(f"⑤ role: done — {record['role']}")
    else:
        print("⑤ role")
        locked = onboard.onboard_status(holder.current.home)["steps"]["template"] or {}
        role = args.role or ask(
            "  role (builder / operator / assistant)",
            str(locked.get("template") or onboard.DEFAULT_TEMPLATE),
        )
        onboard.onboard_role(holder, role=role)
        print(f"  role: {role} (recorded in {home / onboard.FIRST_RUN_FILE})")
    return 0


def _onboard_chat(supervisor_home: Path, role: str) -> int:
    """⑤'s hand-off: attach to a running daemon, or start `skep serve`
    detached and wait for it, then open `skep chat` with the role noted."""
    import subprocess
    import time
    from urllib.parse import urlparse

    import httpx

    from .cli_chat import DEFAULT_URL, TOKEN_HEADER, run_chat
    from .supervisor.serve.auth import TOKEN_FILE

    url = os.environ.get("SKEP_SERVE_URL") or DEFAULT_URL

    def up() -> bool:
        token = supervisor_home / TOKEN_FILE
        if not token.is_file():
            return False
        try:
            response = httpx.get(
                f"{url}/api/status",
                headers={TOKEN_HEADER: token.read_text(encoding="utf-8").strip()},
                timeout=1.0,
            )
        except httpx.HTTPError:
            return False
        return response.status_code == 200

    if not up():
        port = str(urlparse(url).port or 8765)
        log = supervisor_home.parent / "serve.log"
        print(f"starting the daemon: skep serve --port {port} (log: {log})")
        argv = [sys.executable, "-m", "skep", "--home", str(supervisor_home.parent), "serve"]
        subprocess.Popen(
            [*argv, "--port", port],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        deadline = time.monotonic() + 30
        while not up():
            if time.monotonic() > deadline:
                print(
                    "the daemon did not come up — run skep serve, then skep chat", file=sys.stderr
                )
                return 1
            time.sleep(0.5)
    print(f"role: {role} — your first chat opens in role:{role} (skep role list)")
    return run_chat(home=supervisor_home, url=url, role=role)


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
