---
name: opencode
description: delegate a task to the OpenCode CLI backend
---

# Delegate to OpenCode

Tools: dispatch_run, get_run, effective_policy

1. OpenCode rides the shell-worker/CLI backend lane: the operator
   installs the CLI; runs dispatch through the same contract and land
   through the same approval as every backend.
2. `dispatch_run` with the opencode backend where registered (check
   `effective_policy`); brief verification-first.
3. If the backend is not registered on this host, say so and offer the
   default coding worker instead — never shell out to an unregistered
   agent CLI directly.
