---
name: comfyui
description: drive a local ComfyUI instance for image generation via its MCP server
---

# ComfyUI image generation

Tools: register_mcp_server, list_mcp_servers, list_mcp_tools, call_mcp_tool, read_url, read_file

Requires the operator's own running ComfyUI instance — this skill
never installs or starts one uninvited.

1. One-time setup: register the ComfyUI MCP server
   (`register_mcp_server`, `scope='mcp'`) pointing at the local
   instance — the operator confirms the card. Generation/act tools
   stay carded per call until the operator grants specific ones with
   `allow_mcp_tool`. Until registered, say honestly that ComfyUI is
   not connected.
2. Discover what the instance can do (`list_mcp_tools`, available
   checkpoints/workflows) before promising a style — capabilities
   live in the operator's installed models, not in this skill.
3. Generate: workflow + prompt via the MCP tools; show the exact
   positive/negative prompt on the card. Iterate seeds/steps on the
   winning prompt rather than rewriting everything each round.
4. Outputs land in ComfyUI's own output dir; surface the file path
   (loopback web UI `http://127.0.0.1:8188` is the operator's
   preview).
