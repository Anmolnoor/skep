---
name: blogwatcher
description: follow blogs/feeds — a live-chat roundup, on a schedule reminder
---

# Blogwatcher

Tools: read_url, allow_fetch_domain, propose_schedule, list_notes, add_note

Scheduled turns are store-only by design (ADR 0042: no unattended web
fetches), so blogwatching is a LIVE-chat ritual with a scheduled nudge:

1. Keep the blog list as a note (`add_note` "blogwatch: <urls>"); the
   user grants `allow_fetch_domain` once per blog they actually follow.
2. The roundup (live chat): read each granted feed/blog index via
   `read_url`, collect posts newer than the last roundup note, and
   summarize one line each with links. `add_note` the new watermark.
3. To make it recurring, propose a `note`-caste schedule whose text is
   the reminder: "ask me for the blog roundup" — the tick nudges, the
   user opens a live chat, the fetches happen attended.
4. Never claim a feed was checked when its fetch failed — list failures
   honestly.
