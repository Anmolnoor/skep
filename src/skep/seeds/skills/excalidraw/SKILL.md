---
name: excalidraw
description: create diagrams as .excalidraw JSON files the operator can open and edit
---

# Excalidraw diagrams

Tools: dispatch_run, get_run, read_file

An `.excalidraw` file is just JSON — generate it directly and the
operator gets a hand-drawn-style diagram they can keep editing in
excalidraw.com or the VS Code extension. That editability is the point;
for print-clean output use architecture-diagram (SVG) instead.

1. Agree the element inventory in chat first: boxes, labels, arrows,
   groupings.
2. Dispatch a run writing the JSON: `{"type": "excalidraw",
   "version": 2, "elements": [...]}` — rectangles with bound text
   elements, arrows with `startBinding`/`endBinding` to element ids so
   they survive dragging. Grid-align (x/y multiples of 20), spread
   generously; overlap reads as noise in the hand-drawn style.
3. Colors: default palette strokes (`#1e1e1e`, `#e03131`, `#2f9e44`,
   `#1971c2`) — semantic, sparing.
4. Verify: `json.load` the file, every arrow binding resolves to a
   real element id, every box's label present. Lands as a run
   artifact.
