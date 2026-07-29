# ADR 0024 — Inline code execution as a governed script worker (v51-F3)

Date: 2026-07-16 · Status: accepted

## Context

Hermes has `execute_code` — the agent runs Python inline. The Queen had
nothing between "answer from a read tool" and "dispatch a full coding
worker". The gap is real ("loop over all runs and count failures by
reason"), but inline execution is the ultimate shadow permission system if
it lands on the Queen's side of the boundary.

## Decision

**Option A — Queen-side execution: rejected.** A subprocess in the Queen's
(or supervisor's) hands breaks ADR 0019 (arbitrary code as an
uncontrollable surface) and ADR 0005 (no unsandboxed execution, ever).

**Option B — a `script` worker caste: accepted.** `run_code(repo, code,
language?)` dispatches an ephemeral script worker (contract 0.3.2,
additive) into a sandboxed worktree:

- The code is written to a file and the language runtime runs the FILE —
  no shell string interpolation.
- Egress is deny-all (`network=[]`); writes are workspace-only (the
  sandbox enforces both).
- stdout/stderr/exit ride the event stream, an output artifact, and the
  tool result (capped at the v44-F4 transcript bound; the artifact keeps
  the full text).
- **Scripts never land**: `changed_files` is always empty, no patch
  artifact exists, so a script run has no path to a commit
  (patch-as-approval untouched).
- The dispatch blocks until the run finishes — the script's output IS the
  tool result, which is the whole point of an inline-feeling tool.

Gating: `run_code` auto-resolves exactly where a plain `dispatch_run`
would auto-dispatch on that repo — the script envelope is strictly tighter
than what the project's posture already trusts — and cards everywhere
else, with the code verbatim on the card.

The worker is a thin sibling of `shell_worker` (same `cli_adapter`
machinery) and is registered in the default caste map on day one — the
v42 lesson: an unregistered caste silently falls back to the coding worker
and gets rejected.

## Consequences

"Calculate this" costs one sandboxed, fully audited worker run. G10's
spirit holds without a patch to re-verify: the captured output in the
event stream is the evidence, reproducible by re-running the same code.
