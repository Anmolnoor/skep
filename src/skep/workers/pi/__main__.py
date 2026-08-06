"""pi adapter — a thin spec over the shared CLI-agent adapter (v33).

pi (earendil-works/pi) ships no permission-prompt system of its own — it runs
tools unprompted, which is exactly the ADR 0047 posture: confinement is the
sandbox's job, not the agent's prompts. ``-p`` is its headless one-shot; it
does not commit, so the working-tree diff is the patch. Session transcripts
are NOT suppressed: PI_CODING_AGENT_DIR (engines.py) redirects them into the
run's ``.toolchain/pi`` scratch dir, so the agent's full log survives with a
kept worktree (v107-F1) instead of dying on the sandbox's read-only ``~``.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from skep.workers.cli_adapter import AdapterSpec, run_cli_agent_task

WORKER_VERSION = "pi-adapter-0.1.0"
WORKER_CASTE = "coding"

PI_SPEC = AdapterSpec(
    caste=WORKER_CASTE,
    worker_version=WORKER_VERSION,
    command_env="SKEP_PI_CMD",
    default_command=("pi",),
    # pi's non-interactive one-shot: `pi -p <prompt>` edits the tree, prints
    # the response, and exits without committing.
    build_argv=lambda base, instructions: [*base, "-p", instructions],
    plan_steps=("run pi -p", "capture git diff"),
)


def run_pi_task(task_path: Path, out_path: Path) -> int:
    return run_cli_agent_task(task_path, out_path, PI_SPEC)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run pi under Skep's worker contract")
    parser.add_argument("--headless", action="store_true", help="run one contract task and exit")
    parser.add_argument("--task-file", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    if not args.headless:
        parser.error("--headless is required")
    return run_pi_task(args.task_file, args.out)


if __name__ == "__main__":
    raise SystemExit(main())
