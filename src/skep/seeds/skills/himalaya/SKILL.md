---
name: himalaya
description: read mail with the himalaya CLI where installed; sends always card
---

# Himalaya CLI mail

Tools: run_shell, allow_shell_command, call_mcp_tool

The CLI is optional. If `himalaya` is not installed, fall back to the
first-party mail MCP — do not install anything for this.

1. Read recipes ride narrow READ-VERB prefix grants the operator may
   confirm once: `allow_shell_command himalaya envelope list` and
   `allow_shell_command himalaya message read`. Never request a grant
   for the bare binary — that would silently auto-allow
   `himalaya message send` and delete/move, the exact operations the
   mail scope cards.
2. List: `himalaya envelope list -s 20`; read:
   `himalaya message read <id>`. Folder via `-f <folder>`.
3. Anything that mutates the mailbox — send, delete, move, flag —
   runs UNGRANTED on purpose: each invocation cards, and the card's
   full argv shows exactly what leaves (recipient, subject, body for a
   send). Compose, show the card, act only on confirm. Never request a
   shell grant for these.
4. Account credentials live in himalaya's own config, never in chat.
