---
name: dogfood
description: exploratory QA of a web app through the governed browser
---

# Dogfood a web app

Tools: setup_browser, list_mcp_tools, call_mcp_tool, allow_mcp_tool, start_process, read_process_log, stop_process, add_note

1. App not running → `start_process` its dev server (non-repo cwd or
   the built artifact), `read_process_log` for the port.
2. Browser not set up → `setup_browser` (one card). Browsing reads
   (snapshot) flow free; navigation/clicks card until the user grants
   the acting tools with `allow_mcp_tool` — a QA session usually grants
   navigate+click+type once.
3. Explore like a rude first-time user: the happy path, then empty
   states, double-submits, back-button, absurd input lengths, refresh
   mid-flow. Snapshot after each surprise.
4. Findings → one `add_note` per session: repro steps, expected vs
   observed, severity. Fixes are separate dispatches — never patch
   mid-dogfood.
5. `stop_process` the server at the end.
