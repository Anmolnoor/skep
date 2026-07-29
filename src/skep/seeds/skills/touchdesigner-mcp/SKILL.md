---
name: touchdesigner-mcp
description: build and control TouchDesigner networks via its MCP server
---

# TouchDesigner via MCP

Tools: register_mcp_server, list_mcp_servers, list_mcp_tools, call_mcp_tool

Live visuals in the operator's running TouchDesigner instance,
driven through the community TouchDesigner MCP server.

1. One-time setup: the operator installs the MCP component in
   TouchDesigner; register it (`register_mcp_server`, `scope='mcp'`)
   — the card is the operator's confirmation. Mutating tools (create
   /connect/parameter changes) stay carded per call until granted
   individually with `allow_mcp_tool`. Not registered → say so, don't
   improvise.
2. Read the network before touching it (`list_mcp_tools`, node
   queries): TouchDesigner projects are live performances — a wrong
   parameter change is visible on the output NOW. State which nodes a
   change touches on the card.
3. Build incrementally: operators (TOPs/CHOPs/SOPs) one at a time,
   verify each node exists and cooks without errors before wiring the
   next.
4. Keep a text log of the network you built (node names, connections,
   key parameters) as the run's artifact — TD files are binary; the
   log is the reviewable diff.
