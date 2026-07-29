# ADR 0032 — Card auto-deny on timeout (v54-F1)

Date: 2026-07-17 · Status: accepted

## Context

A `chat_actions` row with `status='proposed'` stayed proposed forever.
Nothing in the ticker, the scheduler, or the chat engine touched it
until a human clicked Approve or Deny — if the user walked away, the
composer stayed locked, the model stayed paused, and the chat was stuck
indefinitely (observed in the 2026-07-17 field test).

## Decision

1. **A timeout on proposed cards, deny-only.** The ticker sweeps at the
   start of each tick: every proposed card (any chat, assistant- or
   operator-sourced) older than `card_timeout_seconds` is resolved as
   DENIED with `{"ok": false, "denied": true, "note": "auto-denied:
   card timed out", "auto": true}`. Auto-deny is NOT auto-approve: the
   model never holds the trigger (ADR 0019); a timeout is the human
   failing to pull it, and the safe default is to not execute.

2. **The timeout is an ordinary policy setting.** Default 300 seconds;
   `0` disables the sweep; settable via PUT /api/policy and the carded
   `set_policy` tool, reported by the effective-policy view. Global,
   not per-card — a per-card `timeout_seconds` column is the named
   upgrade path if a field test wants it.

3. **The transcript sees the denial like a manual deny.** The same
   role-`tool` message a manual deny appends, so the model can respond
   naturally on the user's NEXT message. No SSE continuation from the
   ticker: it has no ChatEngine, and a background model continuation
   the user didn't ask for would surprise. If the chat is bound to a
   messenger channel, a best-effort notification is pushed.

4. **The race is handled by resolution finality.** `resolve_chat_action`
   already rejects a second resolution; the sweep catches the error
   per-card and skips — a manual verdict landing first always stands.

## Consequences

- A walked-away-from card unblocks the chat after 5 minutes (server
  side: the messages endpoint stops 409ing, replay drops the card).
  The already-open web page still shows its stale lock until reload —
  there is no push channel to the web UI; accepted for v1.
- The audit shape is unchanged: an auto-deny is a recorded resolution
  with `auto: true` in `result_json`, distinguishable from a human deny.
