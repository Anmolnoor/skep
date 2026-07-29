---
name: nano-pdf
description: small surgical PDF edits — stamp, watermark, crop, one-page fixes
---

# Nano PDF edits

Tools: dispatch_run, get_run, read_file

Requires the `documents` extra (`pypdf`; `uv sync --extra documents`).
For edits too small to deserve a project: one file in, one file out.

1. Keep the dispatch tiny: source path, ONE operation, output path.
   Typical one-liners with `pypdf`: rotate a page, extract pages N–M,
   overlay a stamp/watermark (`page.merge_page(stamp_page)`), crop via
   `page.mediabox`, drop a page.
2. Never edit in place — write `<name>.edited.pdf` beside the source
   so the original survives a bad edit.
3. Verify by reopening: page count, rotation value, or the stamp text
   present on the target page.
4. If the ask is actually content REWRITING (change the words inside),
   say honestly that PDFs don't support that well — regenerate from
   source instead (the pdf skill, step 3).
