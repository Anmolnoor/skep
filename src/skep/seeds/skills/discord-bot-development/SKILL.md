---
name: discord-bot-development
description: build and run a Discord bot — scaffold, token hygiene, live test
---

# Discord bot development

Tools: dispatch_run, start_process, read_process_log, stop_process, read_url

1. Scaffold via `dispatch_run` (discord.py or discord.js matching the
   repo's language): command handler layout, one example slash command,
   a config module reading the token from an ENV VAR — the token never
   enters code, the repo, or the chat transcript.
2. The user creates the bot + token in the Discord developer portal
   themselves (walk them through it; docs via read_url on a granted
   discord.com).
3. Live test: `start_process` the bot with the token in its env,
   `read_process_log` for the gateway READY line, exercise commands in
   a test server, `stop_process` after.
4. Note: skep itself already has a Discord channel — a bot is for
   features beyond skep's own cards/replies; do not rebuild those.
