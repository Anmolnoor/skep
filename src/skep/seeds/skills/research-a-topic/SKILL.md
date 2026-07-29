---
name: research-a-topic
description: look into X — search, read the best sources, answer with citations
---

# Research a topic

Tools: search_web, read_url, allow_fetch_domain, start_research, delegate_analysis

The skill to load when the user says "look into X" / "research Y".

1. `search_web` the question (rephrase once if the hits are weak).
2. Pick the 1-3 most authoritative hits. ONE page that answers directly →
   `read_url` it (one card; markdown mode keeps structure). A site the
   user reads often → offer `allow_fetch_domain` once so future reads
   flow free.
3. Multi-page or multi-source questions: do NOT chain read_url cards —
   propose `start_research` (a governed researcher run with the hosts as
   its allowlist) and summarize its report when it lands.
4. For a compare/judge question, consider `delegate_analysis` with 2
   angles and synthesize.
5. Answer with: the finding, 2-3 cited URLs, and what you did NOT verify.
   Never present an unread snippet as a read source.
