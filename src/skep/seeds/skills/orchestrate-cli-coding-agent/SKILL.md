---
name: orchestrate-cli-coding-agent
description: run several coding-agent backends on one problem and compose
---

# Orchestrate CLI coding agents

Tools: batch_dispatch, await_runs, get_run, delegate_analysis

For a hard problem worth two independent attempts. To decide which
engine a project should PIN instead, use compare-coding-engines.

1. `batch_dispatch` (cap 3) the SAME verification-first brief to
   different backends, one `engine:` per task (`builtin`,
   `claude_code`, `codex`, `aider`) — each in its own worktree, policy,
   and approval; they never see each other (I3). A CLI engine needs the
   project's pinned `verify_command`, and any explicit engine makes the
   batch card.
2. `await_runs` to collect; `get_run` each for the patch + G10 result.
3. Compose as the Queen: compare the patches (`delegate_analysis` with
   a judge brief if the diffs are large), recommend ONE to land, and
   say why the other loses. Never merge patches yourself — pick, or
   re-dispatch with the combined insight.
4. Land the winner through its own approval; the loser's run is
   evidence, not waste — cite it in the recommendation.
