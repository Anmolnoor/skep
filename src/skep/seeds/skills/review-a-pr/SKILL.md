---
name: review-a-pr
description: review a PR or branch diff — correctness first, one finding per line
---

# Review a PR

Tools: list_prs, git_diff, git_log, read_file, delegate_analysis

1. `list_prs` (or the user names a branch) → `git_diff` base..head.
2. `read_file` the changed files AROUND the diff — a hunk that looks fine
   in isolation breaks against its callers; read at least every caller a
   grep of the changed symbol finds.
3. Big diff or high stakes → `delegate_analysis` with two lenses
   (correctness, security/edge-cases) and synthesize.
4. Report: verdict first (approve / needs changes), then findings ranked
   by severity — file:line, what breaks, the concrete failing input.
   Style nits last and clearly marked. Never invent a finding to seem
   thorough; "this looks correct" is a valid review.
