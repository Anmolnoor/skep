---
name: p5js
description: generative art and interactive sketches with p5.js in a single HTML file
---

# p5.js sketches

Tools: dispatch_run, get_run, read_file

Generative art, simulations, interactive toys — one self-contained
`sketch.html` the operator double-clicks. Vendor the p5.js library
INTO the file (the run downloads it once from the granted lane and
inlines it) — no CDN link; the artifact must work offline forever.

1. Agree the sketch idea and interaction (what does mouse/keyboard
   do?) in one sentence each before writing code.
2. Structure: `setup()` (canvas, seed), `draw()` (the loop), noise
   over random for organic motion, `noLoop()` for stills. Seed
   randomness (`randomSeed`) so a liked result is reproducible —
   display the seed on screen.
3. Export: a keypress calls `saveCanvas()` so the operator can keep a
   PNG of the frame they like.
4. Verify: the HTML parses, contains the inlined library and the
   sketch, references zero external URLs. Lands as a run artifact.
