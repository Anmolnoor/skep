"""v72-F5: first-party stdlib-only stdio MCP servers (mail, calendar).

Each server follows the forge contract (single file, JSON-RPC lines over
stdio, mandatory zero-argument ``self_test``) and is registered by the
OPERATOR like any other MCP server — mail under ``scope=email`` so reads
flow as ``email/read`` and sending cards as ``email/send`` through the one
policy engine (I5/I6); no permission logic lives here.
"""
