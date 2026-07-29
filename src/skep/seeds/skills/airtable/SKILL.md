---
name: airtable
description: query and update Airtable bases via the vendor MCP server
---

# Airtable

Tools: register_mcp_server, list_mcp_tools, call_mcp_tool, allow_fetch_domain, read_url

Same shape as Notion: one-time vendor MCP registration (operator
confirms; the personal access token lives in the server config env,
never in chat).

1. Reads: MCP read tools, or `read_url` against the granted domain
   (`allow_fetch_domain api.airtable.com`). List records with
   `filterByFormula`/`view` server-side rather than paging the whole
   base down.
2. Writes (create/update/delete records): MCP-server-or-nothing —
   each write tool cards until the operator grants it with
   `allow_mcp_tool`. Never a curl write; never a shell grant for the
   API.
3. Record-mutation cards show the exact records and fields changing —
   for a bulk change, the full list, in batches of at most 50 per
   card ("batch 2 of 4"), never a summary.
4. Schema questions (what tables/fields exist) are reads — answer from
   the base schema endpoint before proposing any write.
