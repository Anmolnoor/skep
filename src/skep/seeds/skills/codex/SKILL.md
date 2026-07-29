---
name: codex
description: delegate a task to the OpenAI Codex CLI backend
---

# Delegate to Codex

Tools: dispatch_run, get_run, effective_policy

Same shape as the claude-code skill — Codex is another governed worker
backend, never a different trust level.

1. Pick it when the operator has the Codex CLI configured and wants its
   model on this task (or as the second opinion in a batch_dispatch
   pairing two backends on the same brief).
2. `dispatch_run` with `backend='codex'`; `effective_policy` first for
   backend + network allowance.
3. Brief verification-first; land through the normal approval; quote
   G10, not the backend.
