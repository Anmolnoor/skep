---
name: computer-use
description: drive the desktop — via an operator-registered MCP server only
---

# Computer use (adapted)

Tools: list_mcp_servers, register_mcp_server, list_mcp_tools, call_mcp_tool, allow_mcp_tool

skep ships NO desktop-driving capability — this skill is the honest
setup path, not a smuggled one:

1. Check `list_mcp_servers` for a desktop/computer-use server the
   operator already registered. None → explain what registering one
   means (a server that can see the screen and synthesize input) and
   propose `register_mcp_server` only if the user asks — this is the
   highest-blast-radius surface skep can reach.
2. Once registered, the mcp scope governs: screen READS may flow;
   every input-synthesizing tool cards until `allow_mcp_tool` grants
   it by name. Teach that ramp before the first click.
3. Prefer the narrower tool that does the job: the browser (setup_browser)
   covers web tasks with a much smaller blast radius than desktop
   control — steer there first.
