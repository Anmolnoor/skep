---
name: codebase-architecture-analysis
description: deep-read a codebase into a layered architecture report
---

# Codebase architecture analysis

Tools: search_files, read_file, git_log, delegate_analysis, add_note

The deeper sibling of investigate-a-codebase — a written report, not a
first map.

1. Inventory: manifests, entry points, directory sizes (`search_files`
   by glob), and `git_log` hotspots.
2. `delegate_analysis` per layer (storage, domain, transport/UI, infra):
   each analyst reads its layer and reports responsibilities, key types,
   and what it imports from the others.
3. Synthesize: the dependency direction between layers (and every
   violation of it — those are the findings), the load-bearing
   invariants, and the top 3 risks with anchors.
4. Deliver a markdown report (add_note, or a document-caste dispatch to
   write it into the repo) with every claim anchored file:line. Unread
   corners are listed as unread — coverage honesty is part of the
   report.
