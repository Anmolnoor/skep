---
name: prepare-a-release
description: summarize what shipped since the last tag into release notes
---

# Prepare a release

Tools: git_log, git_diff, list_prs, list_runs, read_file

1. `git_log` since the last tag/release ref; `list_prs` merged in the
   window; `list_runs` for what landed through skep approvals.
2. Group by operator impact — features, fixes, breaking changes,
   internal — not by commit order. A commit message is a hint, not a
   release note: `git_diff` anything whose impact is unclear.
3. Breaking changes lead, each with its migration line. Credit every
   change to its PR/run id so the notes are auditable.
4. Deliver markdown notes + the suggested version bump (semver from the
   breaking/feature/fix mix), and say which commits you did NOT inspect.
