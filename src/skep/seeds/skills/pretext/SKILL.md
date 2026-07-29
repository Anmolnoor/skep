---
name: pretext
description: author structured textbooks and course materials in PreTeXt XML
---

# PreTeXt authoring

Tools: dispatch_run, get_run, read_file, search_files

PreTeXt is XML for open textbooks — one source, HTML/PDF/EPUB out.
The toolchain (`pretext` CLI) lives in the run workspace venv.

1. New project: dispatch `pip install pretext && pretext new book` in
   a workspace; the generated skeleton IS the documentation — read it
   before inventing structure.
2. Authoring: one `<chapter>`/`<section>` per file via xinclude;
   `<definition>`, `<theorem>`, `<example>`, `<exercise>` elements
   over visual markup — semantic structure is the whole point of
   PreTeXt; if you're hand-styling, you're fighting it.
3. Math is LaTeX inside `<m>`/`<me>` elements; figures via
   `<image>`/`<latex-image>` (TikZ compiles only when the workspace
   has LaTeX — probe `xelatex --version` functionally first, else
   target HTML output and say so).
4. Build + verify: `pretext build web` must succeed and the output
   index.html contain the chapter titles; `pretext view` is the
   operator's preview. Land through the normal run artifacts.
