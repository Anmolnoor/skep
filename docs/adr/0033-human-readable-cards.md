# ADR 0033 — Human-readable confirmation cards (v54-F3)

Date: 2026-07-17 · Status: accepted

## Context

The confirmation card showed a raw tool name (`dispatch_run`) and raw
JSON args. The user's field-test words: "i don't get a valid info what
it is asking me to approve." The plain-English descriptions already
existed in `TOOL_SPECS` — but only the model ever saw them; the action
event sent `{action_id, tool, args}` and the card rendered JSON.

## Decision

1. **The card carries the spec's description.** A `tool_description(name)`
   lookup over `TOOL_SPECS` feeds a `description` field on every surface
   a card is born or replayed from: the live SSE action event, the
   chat-detail actions (replay), and the command-deck POST response.
   It is derived, never stored — the `chat_actions` row is unchanged.

2. **Args render as labeled key-value pairs**, not raw JSON. String
   values inline and wrapping; nested values (e.g. `batch_dispatch.tasks`)
   stay JSON but under a labeled key. A display concern only — the
   server sends `args` unchanged.

3. **No new trust surface.** The description is the same text the model
   sees in the tool spec — same trust level as the tool name and args
   the card already showed. Per the house rule, tool descriptions are
   load-bearing: keeping them truthful now serves the human AND the
   model.

4. **Messenger channels deferred with a named trigger.** The channel
   confirm-card renderer (`render_confirm_card`) is the shell-command
   card, a different surface; the chat-action card on Telegram gets only
   the tool name. Piping the description through three transports waits
   for a field test showing an unreadable card on a specific channel.

## Consequences

- You can tell what you're approving at a glance; `register_mcp_server`
  or `set_policy` cards explain themselves.
- An unknown tool name yields `description: ""` and the card renders as
  before (no empty line).
