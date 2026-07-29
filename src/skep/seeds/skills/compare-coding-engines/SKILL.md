---
name: compare-coding-engines
description: measure coding engines on one brief and recommend the project's pin (adapted from ECC's agent-eval)
---

# Compare coding engines

Tools: effective_policy, batch_dispatch, await_runs, get_run, git_diff, setup_project

Answers "which engine should this project pin", NOT "solve this hard
problem twice" — that is orchestrate-cli-coding-agent.

1. PRECONDITION: `effective_policy` for the repo must show a pinned
   `verify_command`. Without it the comparison is meaningless — a CLI
   engine's built-in verify is `git diff --check` (ADR 0047), so every
   engine would "pass". Not pinned → say so and offer
   `setup_project` first; do not run the comparison.
2. Write ONE verification-first brief small enough to finish (a real
   bug or a scoped feature, not a toy), then `batch_dispatch` it with
   the SAME instructions and a different `engine:` per task —
   `builtin`, `claude_code`, `codex`, `aider`. Cap 3 per batch. Each
   gets its own worktree and approval; they never see each other (I3).
3. `await_runs`, then `get_run` each. Score on
   `reverification.confirmed` — the SUPERVISOR's re-run of the pinned
   command. The run also carries `verification_outcome`, the worker's
   own claim: ranking engines on that measures which agent is most
   confident, not which is correct (I2). Elapsed = `updated_at` minus
   `created_at`.
4. `git_diff` each patch: did it do what the brief asked, and at what
   size? A confirmed patch three times larger is not the winner by
   default — say what each traded.
5. Recommend one pin with the evidence (confirmed N/M, elapsed, diff
   size) and apply it via `setup_project engine=` — which cards. One
   brief is one data point: say so, and suggest a second brief before
   pinning anything the operator will live with.
6. Land or discard each run through its own approval. A losing run is
   evidence, not waste — cite it.
