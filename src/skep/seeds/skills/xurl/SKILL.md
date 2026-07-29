---
name: xurl
description: X/Twitter reads, and posts that always show the exact text on a card first
---

# X (Twitter) via xurl

Tools: run_shell, allow_fetch_domain, read_url, search_web

A wrong post is public and permanent — the highest-blast-radius side
effect on the shelf. The rule is hard-coded: compose → show the EXACT
final text on the card → post only on confirm.

1. Credentials: the operator's own `xurl` auth (env / its config
   file, 0600) — never pasted into chat; transcripts persist in the
   store.
2. Reads and search: prefer the granted fetch lane
   (`allow_fetch_domain api.x.com`, then `read_url`) or `search_web`
   for public content.
3. Posting, replying, DMs: run UNGRANTED through `run_shell` — every
   invocation cards, and the card's argv contains the verbatim post
   text. That is the safety mechanism, not a formality: posting
   prefixes are never-grantable by design (a grant attempt is refused;
   `xurl` mutations ride flags, so no prefix of it is read-only).
   Never request a shell grant for posting.
4. Show character count with the composed text; for a thread, one card
   per post, numbered, before any of them fires.
5. A DM is outbound content like a post — same verbatim-card rule.
