---
name: plan
description: write an executor-style implementation plan before touching code
---

# Plan

Tools: read_file, search_files, git_log, add_note

For work too large for one dispatch: write the plan FIRST, as if a
different executor will implement it cold.

1. Read everything the change touches (`search_files`, `read_file`) —
   a plan anchored on unread code is fiction. Cite file:line for every
   claim.
2. Structure per fix: observed problem → root cause with anchors → the
   change → tests → acceptance. Order fixes by dependency; name what is
   deliberately out of scope.
3. Each fix should be one dispatch_run-sized unit with its verify step
   stated up front (verification-first: the executor never improvises
   the check).
4. `add_note` the plan (or write it to the repo's plans/ dir via a
   dispatch) and walk the user through the risky parts before executing.
