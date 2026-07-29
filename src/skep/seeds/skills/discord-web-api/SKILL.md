---
name: discord-web-api
description: Discord server administration via REST — reads free, every send or role change cards
---

# Discord web API

Tools: allow_fetch_domain, read_url, register_mcp_server, list_mcp_tools, call_mcp_tool, discord_delete_message, discord_timeout_member

When to use what: chat REPLIES belong to the existing messenger
channel — this skill is for server ADMINISTRATION (channels, roles,
members, moderation) via the REST API or a registered Discord MCP
server.

1. Reads (list channels, members, messages): `read_url` against the
   granted domain (`allow_fetch_domain discord.com`, bot token in the
   header from env — the token never appears in chat), or MCP read
   tools.
2. Every message-send, role change, kick/ban, or channel edit is a
   card showing the exact payload — the message text verbatim, the
   role and member named. Compose, card, act on confirm. Writes go
   through a registered MCP server's tools; never a curl write, and
   never request a shell grant for posting.
3. Moderation that skep already routes first-party: use
   `discord_delete_message` / `discord_timeout_member` — they card
   with the target and reason.
4. State the blast radius on the card when it's a server-wide change
   ("@everyone ping", "role affects 214 members").
