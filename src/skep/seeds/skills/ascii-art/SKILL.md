---
name: ascii-art
description: text banners, ASCII drawings, and image-to-ASCII conversion
---

# ASCII art

Tools: dispatch_run, run_code, read_file

1. Text banners: `pyfiglet` in a run (`run_code` for a quick one,
   dispatch for a file artifact) — pick a font that survives the
   target medium's line width; check the widest line ≤ 80 chars
   unless told otherwise.
2. Image to ASCII: a small script — load with pillow, resize (halve
   height: characters are ~2x taller than wide), map luminance onto a
   ramp like ` .:-=+*#%@`. Pillow rides the `documents`/`ocr` extra;
   otherwise the run venv installs it.
3. Freehand drawings (a rocket, a box diagram): draw it directly in a
   fenced code block — monospace assumptions stated (spaces, never
   tabs).
4. Verify the artifact by re-reading it and checking line-width bounds
   and non-empty output — mangled ASCII art is worse than none.
