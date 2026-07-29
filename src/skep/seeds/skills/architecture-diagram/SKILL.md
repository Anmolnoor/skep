---
name: architecture-diagram
description: generate architecture diagrams as clean SVG artifacts from a codebase or description
---

# Architecture diagrams (SVG)

Tools: dispatch_run, get_run, read_file, search_files, delegate_analysis

1. Ground it first: for a real codebase, map the actual components and
   edges (`search_files`, or `delegate_analysis` for a big repo) —
   never diagram from folklore.
2. Agree the inventory in chat before drawing: boxes (components),
   arrows (data/control flow), and the ONE question the diagram
   answers. A diagram that answers everything answers nothing.
3. Dispatch a coding-caste run that writes hand-authored SVG (or
   graphviz `dot` if the workspace has it — check, don't assume):
   layered layout, left-to-right flow, labels on every edge, a title.
4. Verify by parsing the SVG (well-formed XML, expected node labels
   present) and land it through the normal run artifact approval.
5. Keep the source (`.dot` or the generating script) beside the SVG so
   the next edit regenerates instead of redrawing.
