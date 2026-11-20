# ADR 0067 — a channel admin confirms every card from the messenger

**Status:** accepted (v130-F5)
**Related:** ADR 0019 (the model never holds the trigger), ADR 0059 D6 (HIGH
cards), v41-F2 / v66-F2 (the four channel-confirmable classes), v44-F5
(Discord moderation stays off Discord), v78 (no channel-side landing).
Invariants touched: I5, I6, I8, I11, I13.

## Context

Only four low-risk classes (`dispatch_run`, `scheduled_result_ack`,
`read_url`, `start_research`) could be confirmed from a messenger, and no
setting could widen that. The operator's Discord chats on 2026-11-19 had
raised, beyond those four, `propose_schedule` 9, `run_schedule_now` 7, MCP
register/unregister 15, `land_run` 3, `set_policy` and `promote_tool` cards:
every one meant a trip to the web UI. Away from the home network the web UI
is out of reach, so the card simply waits. The operator asked for the owner's
own messenger to carry every card, with the default for everyone else
unchanged and the owner named during setup.

## Decision

1. **`admin_users` per channel** (a subset of `allowed_users`). Set only from
   the desk: Settings → channels, `PUT /api/channels/{name}`, or
   `skep channel admin <channel> <user id>... | --clear`. There is no chat
   tool and no channel command for it: naming an admin is the operator's own
   act, like `/trust`. Naming one turns `channel_can_confirm` on; switching
   that off later is respected (the master switch still stops every confirm).
2. **The admin may confirm any class, HIGH cards included.** The shared gate
   (`channel_confirmation_decision(admin=True)`) skips the class check for an
   admin press only; the chat and the presser must still be allow-listed.
   Every other allowed user keeps the four.
3. **The server asks again.** An admin press carries the actor
   `channel:<channel>:<chat>:admin:<user>`; `verdict_events` lets a HIGH
   confirm from a channel through only when that user is in the channel's
   stored `admin_users` now (`channel_admin_actor`). Clearing the admin takes
   effect on the next press. The ledger records which admin approved (I13).
4. **Cards on a channel with an admin carry buttons** for every class; a
   non-admin press on one outside the four gets the old refusal. A fold too
   long to show whole stays web-only for everyone (nobody approves what no
   one could read). A card in a chat that read private files is still
   withheld from the messenger (v129-F4): that rule protects data, not
   authority.
5. **Without an admin, a web-only card says how to name one** — the exact
   CLI line, with the user id filled in when the channel has one allowed user.

## Consequences

- This reverses v44-F5 and v78 for the admin only: a stolen admin messenger
  account can approve anything the admin can, including landing. Mitigations:
  the admin is named at the desk, never from the channel; the presser's
  platform-authenticated id is checked, not the room; the server re-checks
  the stored list on every confirm; the ledger names the admin; one command
  clears it. The docs advise naming an admin only on an account with its own
  second factor.
- I6 holds: a human still presses every confirm, and timeouts still deny.
- I11 holds: the messenger stays a doorway to the local core; nothing new
  listens on the network.
