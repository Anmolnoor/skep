---
name: popular-web-designs
description: recreate proven web design patterns — hero, pricing, bento, docs — as starting points
---

# Popular web design patterns

Tools: dispatch_run, read_file, read_url

A pattern library in prose: name the pattern, get a faithful
self-contained HTML implementation (html-design rules: inline CSS, no
CDN, system fonts).

1. The catalog — pick by job, not by fashion: centered hero +
   social-proof row (SaaS landing) · bento grid (feature overview) ·
   three-tier pricing with a highlighted middle · sticky-sidebar docs
   layout · linear checkout/waitlist form · dark dashboard shell
   (sidebar + stat cards) · long-form article (65ch measure,
   generous leading).
2. Ask for the real content first (product name, actual features,
   actual prices) — the pattern is the skeleton; placeholder content
   makes review meaningless.
3. Dispatch one run per page; state the pattern name in the file's
   header comment so later edits know the intent.
4. Verify like html-design: parses, real copy present, zero external
   references. Adaptation beats fidelity — drop pattern parts the
   content doesn't need.
