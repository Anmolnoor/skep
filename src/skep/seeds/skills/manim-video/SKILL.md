---
name: manim-video
description: render math and explainer animations with Manim as run artifacts
---

# Manim animations

Tools: dispatch_run, get_run, read_file, read_process_log

3Blue1Brown-style animation as a script-caste dispatch. Manim +
ffmpeg live in the run workspace venv (manim needs ffmpeg — the run
checks `ffmpeg -version` first, functional probe over import check).

1. Storyboard in chat before rendering: scenes, what appears when,
   the one concept each scene teaches. Rendering is the expensive
   iteration loop — words are cheap.
2. Dispatch: venv + `pip install manim`, one `Scene` subclass per
   storyboard scene, render at `-ql` (480p) for review passes —
   `-qh` only after the operator approves the draft.
3. Composition rules: one idea on screen at a time, `Write`/`Create`
   for introductions, `Transform` for change, `FadeOut` before the
   next idea; every animation ≥0.8s — speed-read math teaches
   nothing.
4. Verify: the mp4 exists with nonzero duration (ffprobe), one file
   per scene; land as run artifacts. Long renders: follow via
   `read_process_log` rather than silence.
