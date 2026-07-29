---
name: google-workspace
description: Gmail, Calendar, Drive — through the first-party MCP servers and granted fetch
---

# Google Workspace

Tools: list_mcp_servers, list_mcp_tools, call_mcp_tool, allow_fetch_domain, read_url, list_approvals

Mail and calendar ride the first-party MCP servers skep already ships
— check `list_mcp_servers`; if absent, the one-time setup is a server
registration the operator confirms (see the MCP lifecycle: act tools
stay carded until `allow_mcp_tool`).

1. Email: reads flow per the email-scope policy; every SEND is a card
   with the full recipient/subject/body — compose, show, send only on
   confirm. Never promise "sent" while the card is pending.
2. Calendar: list/read free per policy; event creation/changes card
   with the exact event payload.
3. Drive/Docs/Sheets reads: `read_url` against the granted domain
   (`allow_fetch_domain googleapis.com` once, operator-confirmed).
   Writes to Drive are MCP-server-or-nothing — never a raw REST write.
4. Exported artifacts (a Sheet as xlsx, a Doc as pdf) land in the
   workspace and continue in the docx/xlsx/pdf skills.
