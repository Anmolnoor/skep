"""Aider adapter — a thin spec over the shared CLI-agent adapter (v33).

Aider auto-commits by default; skep workers must NEVER commit (landing is the
only commit). ``--no-auto-commit`` keeps Aider's changes in the working tree so
the adapter captures them as the patch — and the worker cannot slip a commit
past the patch-as-approval guarantee.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from skep.workers.cli_adapter import AdapterSpec, run_cli_agent_task

WORKER_VERSION = "aider-adapter-0.1.0"
WORKER_CASTE = "coding"

AIDER_SPEC = AdapterSpec(
    caste=WORKER_CASTE,
    worker_version=WORKER_VERSION,
    command_env="SKEP_AIDER_CMD",
    default_command=("aider",),
    # Non-interactive, no self-commit (skep captures the working-tree diff and
    # lands it via approval), no analytics.
    build_argv=lambda base, instructions: [
        *base,
        "--message",
        instructions,
        "--yes-always",
        "--no-auto-commit",
        "--no-analytics",
    ],
    plan_steps=("run aider --message (no auto-commit)", "capture git diff"),
)


def run_aider_task(task_path: Path, out_path: Path) -> int:
    return run_cli_agent_task(task_path, out_path, AIDER_SPEC)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run Aider under Skep's worker contract")
    parser.add_argument("--headless", action="store_true", help="run one contract task and exit")
    parser.add_argument("--task-file", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    if not args.headless:
        parser.error("--headless is required")
    return run_aider_task(args.task_file, args.out)


if __name__ == "__main__":
    raise SystemExit(main())
