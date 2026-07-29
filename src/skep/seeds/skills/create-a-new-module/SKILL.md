---
name: create-a-new-module
description: scaffold a module + its test in the repo's own style
---

# Create a new module

Tools: search_files, read_file, dispatch_run

1. `search_files` + `read_file` the nearest sibling module — its
   imports, docstring shape, error style, and test layout ARE the spec.
2. `dispatch_run`: "Create <path> providing <one-line contract>, plus
   <test path> covering the public surface. Match the conventions of
   <sibling>. Verify: the new tests pass and the suite stays green."
3. Smallest useful surface first — no speculative options, no config for
   values that never change. The user can always ask for more.
