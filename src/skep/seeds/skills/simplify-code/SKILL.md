---
name: simplify-code
description: three-lens cleanup — delete, reuse, flatten — without behavior change
---

# Simplify code

Tools: read_file, delegate_analysis, dispatch_run

1. `read_file` the target; then `delegate_analysis` with three lenses:
   (a) what can be DELETED (dead code, speculative abstraction, config
   for constants), (b) what REINVENTS the stdlib or an existing helper
   in this repo, (c) what nesting/indirection can be FLATTENED.
2. Synthesize into a ranked cut list — biggest deletion first. Every
   item: location, what to cut, what replaces it (often: nothing).
3. Apply via `dispatch_run` with the hard constraint: "Behavior
   unchanged. Verify: the existing suite passes untouched — do not edit
   tests to make removals fit."
4. Report lines removed vs added; a simplification that grew the code
   failed.
