---
name: fix-a-lint-error
description: turn a lint/type error into a scoped one-file fix
---

# Fix a lint error

Tools: read_file, search_files, quick_edit, dispatch_run

1. Read the error message the user pasted; `search_files` for the exact
   code if no file:line was given; `read_file` the site.
2. One file, mechanical fix → `quick_edit` (repo, file, plain
   instruction). Multiple files or a rule-config question →
   `dispatch_run` with the full error list in the brief.
3. The brief always names the check: "Verify: <the lint/type command>
   exits clean." A fix that silences the tool without addressing the
   cause (bare ignore comments) needs the user's explicit ok first.
