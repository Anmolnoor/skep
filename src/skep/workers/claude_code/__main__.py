"""Claude Code adapter — a thin spec over the shared CLI-agent adapter (v33)."""

from __future__ import annotations

import argparse
from pathlib import Path

from skep.workers.cli_adapter import AdapterSpec, run_cli_agent_task

WORKER_VERSION = "claude-code-adapter-0.1.0"
WORKER_CASTE = "coding"

CLAUDE_SPEC = AdapterSpec(
    caste=WORKER_CASTE,
    worker_version=WORKER_VERSION,
    command_env="SKEP_CLAUDE_CODE_CMD",
    default_command=("claude",),
    # Claude Code's headless one-shot: --print runs and exits. acceptEdits is
    # load-bearing (v94-F1): --print cannot prompt, so without it every file
    # write is rejected and the engine can never produce a patch. Confinement
    # is the sandbox's job, not Claude's prompts (ADR 0047).
    build_argv=lambda base, instructions: [
        *base,
        "--permission-mode",
        "acceptEdits",
        "--print",
        instructions,
    ],
    plan_steps=("run claude --print", "capture git diff"),
)


def run_claude_code_task(task_path: Path, out_path: Path) -> int:
    return run_cli_agent_task(task_path, out_path, CLAUDE_SPEC)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run Claude Code under Skep's worker contract")
    parser.add_argument("--headless", action="store_true", help="run one contract task and exit")
    parser.add_argument("--task-file", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    if not args.headless:
        parser.error("--headless is required")
    return run_claude_code_task(args.task_file, args.out)


if __name__ == "__main__":
    raise SystemExit(main())
