---
name: notion
description: read and update Notion pages and databases via the vendor MCP server
---

# Notion

Tools: register_mcp_server, list_mcp_servers, list_mcp_tools, call_mcp_tool

Notion is MCP-only. `read_url` sends no headers and no auth, so it can
never reach the Notion API; do not try it.

One-time setup: register Notion's MCP server (`register_mcp_server` —
the operator confirms the card; the token is one of its `secret_names`,
set with `skep mcp secret set <server_id> <NAME>`, never chat). Until then, honestly say Notion is not connected.

1. Reads: the server's read tools through `call_mcp_tool`
   (`list_mcp_tools` shows them and what policy decides for each).
2. Writes (create/update page, database rows): every write tool call
   is an mcp-scope card until the operator grants that specific tool
   with `allow_mcp_tool` — never ask for a shell grant to reach the
   API, and never use curl for a write; a curl lane is a generic
   mutation primitive no scope governs.
3. Show the exact content of a page write on the card (title +
   body/properties), same rule as any outbound content.
4. Database queries: filter server-side (the query payload), not by
   downloading the whole database.
