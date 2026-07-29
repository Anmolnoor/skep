---
name: powerpoint
description: generate PowerPoint decks from notes or outlines
---

# Slide decks (.pptx)

Tools: dispatch_run, get_run, read_file

Requires the `documents` extra (`uv sync --extra documents` installs
`python-pptx`). The dispatch briefing states whether it is present.

1. First turn the source material into a slide outline (title +
   bullets per slide, ≤6 bullets, ≤12 words each) and show it — the
   outline is the cheap thing to correct; the deck is not.
2. Dispatch a coding-caste run with the approved outline, output path,
   and `Must include:` acceptance terms (deck title, slide count, one
   known bullet).
3. The worker builds with `python-pptx` (title layout for slide 1,
   title+content for the rest) and verifies by reopening the file and
   walking `presentation.slides` against the outline.
4. Want visual polish instead of Office format? A static HTML deck is
   often better — that recipe lives in the html-design skill.
