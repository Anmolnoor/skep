---
name: notion
description: read and update Notion pages and databases via the vendor MCP server
---

# Notion

Tools: register_mcp_server, list_mcp_tools, call_mcp_tool, allow_fetch_domain, read_url

One-time setup: register Notion's MCP server (`register_mcp_server` —
the operator confirms the card; the token rides the server config env,
never chat). Until then, honestly say Notion is not connected.

1. Reads: MCP read tools, or `read_url` against the granted domain
   (`allow_fetch_domain api.notion.com`, `Notion-Version` header).
2. Writes (create/update page, database rows): MCP-server-or-nothing.
   Every write tool call is an mcp-scope card until the operator
   grants that specific tool with `allow_mcp_tool` — never ask for a
   shell grant to reach the API, and never use curl for a write; a
   curl lane is a generic mutation primitive no scope governs.
3. Show the exact content of a page write on the card (title +
   body/properties), same rule as any outbound content.
4. Database queries: filter server-side (the query payload), not by
   downloading the whole database.
