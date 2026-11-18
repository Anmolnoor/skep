---
name: summarize-a-youtube-video
description: fetch a video transcript and turn it into a summary, thread, or notes
---

# Summarize a YouTube video

Tools: list_mcp_tools, call_mcp_tool, list_plugins, search_web

Needs the forged `yt_transcript` plugin, whose tool is `get_transcript`
(list_plugins shows it; if absent, tell the user it can be promoted from
the shipped forge seed; if it only offers the old `yt_transcript` tool, it
is an old installed copy and `promote_tool` takes the shipped fix).

1. `call_mcp_tool` get_transcript with the video URL/id → plain-text
   transcript (a read; youtube domains only).
2. If it errors, say which error it was, then fall back: `search_web` the
   video title for coverage, and say the summary is secondhand.
   - `NO_CAPTIONS`: the video has no transcript.
   - `EMPTY_CAPTIONS` or "refused": YouTube is blocking the tool for now;
     retrying later may work.
   - "re-forging": YouTube changed shape; tell the user the tool needs
     re-forging. An installed copy that reports re-forging takes the
     shipped fix via `promote_tool` (one card, after its trial).
   - Tool missing: say it can be promoted (above).
3. Deliver what was asked: a tight summary (key claims + timestamps if
   present), a thread (one idea per post), or study notes. Quote sparingly
   and mark paraphrase vs quote honestly.
