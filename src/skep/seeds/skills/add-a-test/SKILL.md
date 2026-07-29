---
name: add-a-test
description: add a test for existing behavior via a scoped worker run
---

# Add a test

Tools: read_file, search_files, dispatch_run

1. `read_file` the target module; `search_files` for its existing tests
   (mirror their file, fixtures, and naming — never invent a new style).
2. `dispatch_run` with a verification-first brief: "Add test(s) for
   <behavior> in <test file>, following the conventions of <neighbor
   test>. Verify: the new test fails when <behavior> is broken and the
   suite passes as-is."
3. When it lands, tell the user what the test pins and how it was
   verified (G10 re-verification runs supervisor-side; quote its result,
   not the worker's claim).
