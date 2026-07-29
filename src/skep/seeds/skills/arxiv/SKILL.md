---
name: arxiv
description: find and digest arXiv papers by topic, author, or id
---

# arXiv research

Tools: search_web, read_url, allow_fetch_domain, start_research

1. `search_web` with `site:arxiv.org` plus the topic/author (arXiv's
   own search is also linkable: arxiv.org/list/<category>/recent).
2. `read_url` the /abs/<id> page for the abstract (offer
   `allow_fetch_domain arxiv.org` once — a researcher reads arXiv
   daily). The HTML version (arxiv.org/abs → "HTML" link) reads better
   than the PDF; say when only a PDF exists and read_url can't do it
   justice.
3. A literature sweep (many papers, cross-citations) →
   `start_research` with arxiv.org allowlisted, not a card chain.
4. Digest format: claim of the paper, method in one line, the result
   number that matters, and whether it's peer-reviewed or preprint-only
   (arXiv is preprints — always say so).
