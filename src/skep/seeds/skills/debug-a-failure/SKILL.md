---
name: debug-a-failure
description: from traceback to root cause to a verified fix
---

# Debug a failure

Tools: read_file, search_files, run_code, dispatch_run, get_run

1. Read the traceback bottom-up: the deepest in-project frame is the
   scene of the crime. `read_file` it with surrounding context.
2. Form ONE hypothesis and test it cheaply: `run_code` (fast=true for a
   pure reproduction) or read the callers `search_files` finds. Do not
   patch on vibes — reproduce first (a fix without a reproduced cause is
   a guess wearing a diff).
3. Fix at the root: `dispatch_run` briefed with the reproduction AS the
   verify step ("Verify: <the failing command> now passes").
4. `get_run` after landing to quote the supervisor-side verification.
