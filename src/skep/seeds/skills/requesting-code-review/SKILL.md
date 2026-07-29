---
name: requesting-code-review
description: pre-landing review pass — security, correctness, and the checklist
---

# Requesting code review

Tools: get_run, git_diff, read_file, delegate_analysis, approve_review, deny_review

Before approving a landing, when the change deserves more than a skim:

1. `get_run` the run; `git_diff` its patch; `read_file` enough context
   around every hunk to judge it against its callers.
2. Checklist: inputs validated at trust boundaries? errors swallowed?
   secrets/credentials touched? new dependencies? anything the brief
   did not ask for (scope creep is a finding)?
3. Substantial diff → `delegate_analysis`: one correctness lens, one
   security lens; synthesize.
4. Verdict through the ledger: `approve_review` (optionally with a
   note) or `deny_review` with a reason that TEACHES — the worker's
   next attempt reads it. Never approve on the worker's self-report;
   quote the supervisor-side re-verification.
