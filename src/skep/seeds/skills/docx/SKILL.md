---
name: docx
description: produce and edit Word .docx documents as run artifacts
---

# Word documents (.docx)

Tools: dispatch_run, get_run, list_runs, read_file

Requires the `documents` extra (`uv sync --extra documents` installs
`python-docx`). The dispatch briefing states whether it is present —
if missing, say so and give the install line instead of improvising.

1. Dispatch a coding-caste run: instructions name the output path
   (e.g. `report.docx`), the content outline, and a `Must include:`
   line with the acceptance terms.
2. The worker writes the document with `python-docx` and verifies by
   REOPENING the artifact (`Document("report.docx")`) and checking the
   acceptance terms appear — never by "the script exited 0".
3. Editing an existing docx: `Files:` names it; the worker loads,
   edits paragraphs/tables in place, saves, and re-verifies.
4. The artifact lands in the workspace and reaches the operator
   through the normal run-output approval — never pushed anywhere.
