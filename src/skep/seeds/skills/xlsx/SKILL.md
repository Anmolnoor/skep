---
name: xlsx
description: build and analyze Excel .xlsx spreadsheets as run artifacts
---

# Spreadsheets (.xlsx)

Tools: dispatch_run, get_run, read_file

Requires the `documents` extra (`uv sync --extra documents` installs
`openpyxl`). The dispatch briefing states whether it is present.

1. Dispatch a coding-caste run: name the output file, the sheet/column
   layout, and a `Must include:` line (headers, a known cell value, a
   formula) as the acceptance terms.
2. Build with `openpyxl`; formulas go in as strings (`=SUM(B2:B10)`)
   — openpyxl does not evaluate them, so verify VALUES you computed in
   python and verify FORMULAS by re-reading the cell's text.
3. Analysis of an existing sheet: `Files:` names it; load read-only,
   compute, and land the answer in the run summary plus a results
   sheet if asked.
4. Verify by reopening the workbook and asserting the acceptance
   terms; the artifact reaches the operator via the run output.
