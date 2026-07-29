---
name: pdf
description: read, merge, split, and extract from PDF files
---

# PDF operations

Tools: dispatch_run, get_run, read_file, search_files

Requires the `documents` extra (`uv sync --extra documents` installs
`pypdf`). The dispatch briefing states whether it is present.

1. Reading/extracting: dispatch a coding-caste run; `pypdf`'s
   `PdfReader` extracts text per page. Scanned PDFs have no text layer
   — if extraction returns empty pages, route to the
   ocr-and-documents skill instead of reporting "empty document".
2. Merge/split/rotate/reorder: `PdfWriter` with pages appended from
   readers; verify by reopening the output and checking page count
   (and spot-text where a text layer exists).
3. Generating a NEW pdf: draft the content as markdown first (document
   caste), then convert in a run — state which converter the workspace
   actually has; never assume LaTeX exists.
4. Form filling: `PdfWriter.update_page_form_field_values`; verify by
   re-reading the field values from the written file.
