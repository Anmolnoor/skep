---
name: postmortem-a-run
description: reconstruct a run that went wrong from the store and audit trail, name the surface that failed, fix that one
---

# Postmortem a run

Tools: get_run, list_runs, list_approvals, effective_policy, git_diff, search_chats, read_file, dispatch_run, add_note

Starts from A RUN, not a traceback in a repo — for that use
`debug-a-failure`, then `systematic-debugging` when the quick look
fails. This is the failure the operator actually reports: "it said it
was done and nothing landed."

1. Fix the run id and the OBSERVED failure in one sentence. "It didn't
   work" is not an observation.
2. `get_run`: state transitions, commands, approvals, verification AND
   reverification. Read them before forming a theory (I10).
3. Name which of the four surfaces failed — the answer is almost never
   "the model":
   - **the brief** — ambiguous, or its verify step was never stated;
   - **policy** — a command was gated, so the work stopped at a wall,
     not a bug; `effective_policy` says what the run got and why;
   - **the worker** — it did the work and failed its own verification;
   - **the supervisor** — the worker claimed success and G10's re-run
     disagreed. Then the CLAIM is the defect and the gate did its job
     (I2).
4. Follow the trail rather than guessing: `resumed_as` pointers,
   `list_approvals` → `recently_resolved` for a verdict resolved in
   another face, `search_chats` for the dispatch that started it,
   `git_diff` for what the patch actually contained.
5. Root cause with an anchor: `file:line` when it is code, the verbatim
   sentence when it is the brief, the rule name when it is policy.
6. The fix follows the surface. Brief → re-dispatch with the corrected
   verify step stated up front. Policy → the one named grant, which
   cards. Code → `dispatch_run` with the regression check in the brief.
7. `add_note` the postmortem. The same shape twice is a missing skill,
   not bad luck — say so.
