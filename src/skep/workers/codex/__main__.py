"""Codex CLI adapter — a thin spec over the shared CLI-agent adapter (v33)."""

from __future__ import annotations

import argparse
from pathlib import Path

from skep.workers.cli_adapter import AdapterSpec, run_cli_agent_task

WORKER_VERSION = "codex-adapter-0.1.0"
WORKER_CASTE = "coding"

CODEX_SPEC = AdapterSpec(
    caste=WORKER_CASTE,
    worker_version=WORKER_VERSION,
    command_env="SKEP_CODEX_CMD",
    default_command=("codex",),
    # Codex's non-interactive one-shot: `codex exec <prompt>` edits the tree and
    # exits without committing, so the working-tree diff is the patch.
    # ES-F220: its own sandbox is OFF. Read-only (the default) refuses every
    # edit (ES-F128), and any codex sandbox starts its own bubblewrap per
    # shell call, which the kernel refuses inside skep's Landlock domain.
    # skep's sandbox is the wall (ADR 0047): external engines are forced
    # into it (v94-F4), and requires_sandbox refuses a launch outside it.
    build_argv=lambda base, instructions: [
        *base,
        "exec",
        "--sandbox",
        "danger-full-access",
        instructions,
    ],
    plan_steps=("run codex exec", "capture git diff"),
    requires_sandbox=True,
)


def run_codex_task(task_path: Path, out_path: Path) -> int:
    return run_cli_agent_task(task_path, out_path, CODEX_SPEC)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Codex CLI under Skep's worker contract")
    parser.add_argument("--headless", action="store_true", help="run one contract task and exit")
    parser.add_argument("--task-file", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    if not args.headless:
        parser.error("--headless is required")
    return run_codex_task(args.task_file, args.out)


if __name__ == "__main__":
    raise SystemExit(main())
