---
name: html-design
description: landing pages, decks, and prototypes as self-contained static HTML
---

# HTML design

Tools: dispatch_run, get_run, read_file

Landing pages, pitch decks, dashboards, prototypes — one
self-contained HTML file per artifact: inline CSS, system fonts, no
build step, no CDN (the skep UI itself is no-build; same discipline).

1. Brief before build: audience, the one action the page drives, tone
   (3 adjectives), and real copy — lorem ipsum reviews teach nothing.
2. Design choices worth defending: a real palette (2 colors + neutrals),
   type scale (1.25 ratio), generous whitespace, one accent used
   sparingly. Responsive via flexbox/grid and `max-width`; readable at
   375px and 1440px.
3. Decks: one `<section>` per slide, scroll-snap navigation, arrow-key
   handler in a small inline script — print stylesheet gives free PDF
   export.
4. Dispatch writes the file; verify it parses, contains the brief's
   copy verbatim, and references zero external URLs. Lands as a run
   artifact the operator opens directly in a browser.
