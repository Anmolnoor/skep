---
name: summarize-a-youtube-video
description: fetch a video transcript and turn it into a summary, thread, or notes
---

# Summarize a YouTube video

Tools: list_mcp_tools, call_mcp_tool, list_plugins, search_web

Needs the forged `yt_transcript` plugin, whose tool is `get_transcript`
(list_plugins shows it; if absent, tell the user it can be promoted from
the shipped forge seed; if it only offers the old `yt_transcript` tool, it
was forged before the rename and needs re-forging).

1. `call_mcp_tool` get_transcript with the video URL/id → plain-text
   transcript (a read; youtube domains only).
2. If the transcript tool is missing or the video has none, fall back:
   `search_web` the video title for coverage, and say the summary is
   secondhand.
3. Deliver what was asked: a tight summary (key claims + timestamps if
   present), a thread (one idea per post), or study notes. Quote sparingly
   and mark paraphrase vs quote honestly.
