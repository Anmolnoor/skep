---
name: sketch
description: quick visual sketches — wireframes, flows, layouts — as low-fi SVG
---

# Sketch — low-fi visuals

Tools: dispatch_run, run_code, read_file

The visual equivalent of a napkin drawing: wireframes, screen flows,
layout ideas — deliberately rough so nobody mistakes it for a final
design (that's excalidraw's or html-design's job).

1. One question per sketch ("where does the sidebar go?"), agreed in
   chat first.
2. Hand-authored SVG, low-fi on purpose: boxes with 2px borders,
   crossed diagonals for image placeholders, gray bars for text
   lines, real labels only where the answer lives. Monochrome plus
   ONE accent marking the thing the sketch is about.
3. Small enough for `run_code`; multi-screen flows become a dispatch
   writing one SVG per screen plus an arrows overview.
4. Verify: parses as XML, the labeled elements from the brief are
   present. Iterate in words ("move nav left, drop the footer")
   before regenerating — the sketch is cheap, the conversation is the
   design.
