---
name: google-workspace
description: Gmail, Calendar, Drive — through the first-party MCP servers
---

# Google Workspace

Tools: list_mcp_servers, list_mcp_tools, call_mcp_tool, list_approvals

Google Workspace is MCP-only. `read_url` sends no headers and no auth,
so it can never reach a Google API or a private Doc; do not try it.

Mail, calendar and Drive ride the first-party MCP servers — check
`list_mcp_servers`; if one is absent, the one-time setup is a server
registration the operator confirms (see the MCP lifecycle: act tools
stay carded until `allow_mcp_tool`). Until then, honestly say that
service is not connected.

1. Email: reads flow per the email-scope policy; every SEND is a card
   with the full recipient/subject/body — compose, show, send only on
   confirm. Never promise "sent" while the card is pending.
2. Calendar: list/read free per policy; event creation/changes card
   with the exact event payload.
3. Drive/Docs/Sheets: the Drive MCP server's read tools; writes are
   MCP-server-or-nothing — never a raw REST write.
4. Exported artifacts (a Sheet as xlsx, a Doc as pdf) land in the
   workspace and continue in the docx/xlsx/pdf skills.
