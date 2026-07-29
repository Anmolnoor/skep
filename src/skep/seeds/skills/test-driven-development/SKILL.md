---
name: test-driven-development
description: RED-GREEN-REFACTOR enforced through worker briefs
---

# Test-driven development

Tools: dispatch_run, read_file, get_run

Brief workers to work test-first when the user wants TDD:

1. RED: dispatch "Write the test for <behavior> FIRST and run it —
   confirm it FAILS for the right reason (assertion, not import error).
   Then implement."
2. GREEN: the same brief continues "implement the minimum that passes;
   run the test again. Verify: the new test passes AND the full suite
   stays green."
3. REFACTOR: only after green, and only with the tests as the net.
4. Review the landed patch (`read_file` the test): does the test pin
   BEHAVIOR (survives refactors) rather than implementation (breaks on
   any change)? A test asserting on internals gets flagged back.
