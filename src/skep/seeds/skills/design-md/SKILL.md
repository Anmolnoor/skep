---
name: design-md
description: write a DESIGN.md — the visual/interaction contract for a project, before code
---

# DESIGN.md

Tools: dispatch_run, read_file, search_files, quick_edit

A one-file design contract that outlives moods: tokens, voice, and
rules the next contributor (human or worker) can apply without taste.

1. Read what exists first — current CSS/tokens, screenshots, the
   repo's README — and inventory the de-facto design before
   prescribing one.
2. Sections, in order: Principles (3, each with a "so we don't"
   counter-example) · Tokens (color/type/spacing as copy-pasteable
   variables) · Components (the 5–8 that exist, with states) · Voice
   (how UI text talks, with a good/bad pair) · Never (the explicit
   don'ts).
3. Every rule concrete enough to lint by eye: "primary #2563eb on
   white, 4.5:1 minimum", not "accessible blues".
4. Land it via the normal patch approval (document caste or
   `quick_edit` for a small repo doc); future UI dispatches then cite
   DESIGN.md in their instructions — that's the file's whole job.
