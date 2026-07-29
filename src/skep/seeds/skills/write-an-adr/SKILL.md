---
name: write-an-adr
description: record an architecture decision with its forces, its alternatives and its costs (adapted from ECC's architecture-decision-records)
---

# Write an ADR

Tools: search_files, read_file, git_log, quick_edit, dispatch_run, add_note

`plan` writes an executor plan; `design-md` writes a UI contract.
Neither records a DECISION with the alternatives it beat and the price
it charges.

1. Find the repo's own convention first — `search_files` for
   `docs/adr/`, `doc/adr/`, `architecture/decisions/` — and follow it.
   No convention → propose `docs/adr/NNNN-slug.md` once and ask, rather
   than inventing a tree in someone else's repo.
2. Take the NUMBER LAST, from a fresh listing. An unlanded plan can
   already claim one; skep's own v54 review caught a 0028 collision
   exactly that way.
3. Four sections. **Status** · **Question** — the forces, with anchors;
   a decision with no stated pressure is a preference · **Decision** —
   what is chosen, in the present tense · **Consequences**.
4. Name the alternatives that lost, and WHY. An ADR whose only content
   is the winner is a changelog entry.
5. Consequences carry the costs and the ceiling accepted, not only the
   wins. A uniformly positive Consequences section means the decision
   was never weighed.
6. One decision per ADR. An accepted ADR is never edited into a
   different decision — a later one supersedes it and BOTH stay (I8).
7. Land it through the normal patch path: `quick_edit` for a one-file
   doc, or a `dispatch_run` on a document-caste brief. The ADR lands as
   an approval like any other change (I1). `add_note` the number and the
   decision so the next search finds it.
