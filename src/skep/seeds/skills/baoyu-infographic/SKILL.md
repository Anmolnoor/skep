---
name: baoyu-infographic
description: turn an article or dataset into a single self-contained infographic page
---

# Infographic (baoyu style)

Tools: dispatch_run, get_run, read_file, read_url

One idea, one page, no dependencies: a self-contained HTML file
(inline CSS/SVG, no CDN links) that explains one thing visually.

1. Distill first, in chat: the ONE takeaway, 3–5 supporting facts,
   and the numbers with their sources. If the source is a URL, read
   it (`read_url`) — never illustrate an unread article.
2. Structure: headline stating the takeaway → visual core (an SVG
   chart or flow, hand-authored, real numbers) → fact row → source
   line. Cut anything that doesn't serve the takeaway.
3. Dispatch a coding-caste run writing `infographic.html`: system
   font stack, 2–3 colors, inline SVG for charts (bars/lines scaled
   to the actual data — a decorative chart with fake proportions is
   disinformation with polish).
4. Verify: the HTML parses, every number in the brief appears, no
   external resource URLs anywhere in the file. Lands as a run
   artifact.
