# skep in your pocket — the mobile story (v72-F6)

**Can I approve from my phone? Yes** — four low-risk action classes for
every allowed user, and **every card** for the channel's admin (v130-F5,
ADR 0067). Without an admin, everything else routes to the web UI, and the
phone message carries that URL and how to name an admin.

There is no skep app, on purpose: the Discord and Telegram apps you
already carry ARE the remote face (I11 — remote channels are outbound
conveniences layered on the local core). skep serve runs at home; the
messenger is a doorway to it.

## Setup, once

1. Configure a channel (Settings → channels): Discord (needs the
   MESSAGE_CONTENT intent; supports `require_mention`, auto-threads, and
   a user allowlist — v44-F1, fail closed), Telegram, or Slack.
2. Flip `channel_can_confirm` ON for it (default OFF — a binding starts
   as an entrance, never a trigger).
3. Both the chat AND the pressing user must be allow-listed; a bystander
   in a group chat fails closed (v41-F2).
4. Away from home a lot? Name yourself the channel's admin so every card
   reaches you: `skep channel admin telegram <your user id>` (or Settings →
   channels → admin users). The admin must already be an allowed user, and
   naming one turns confirming on. `--clear` removes it.

## While skep works (v130-F6)

A message turn shows one status line in the conversation — "🤔 thinking…",
then "🔧 working: <tool>…" as each tool runs — edited in place and deleted
when the reply lands. Telegram and Discord also show "typing…" (Slack's Web
API has no bot typing, so the status line is the signal there). The status
message is sent silently, and a failing status call never delays the reply.

## Is it actually working? (v87-F3)

"Nothing arrives on Discord" has one honest answer, not a guess:

```sh
skep channel status
```

One line per channel: **never configured** / configured-but-disabled /
enabled, secret present or MISSING, the last delivery attempt with its
result, and (Discord) the last gateway session outcome. The same fields
ride `GET /api/channels` (`configured`, `last_delivery`, `gateway`) and
the Settings page. Every expected-but-missed delivery also logs its
reason to `serve.log` — a channel that was never configured says so in
those words, everywhere, instead of presenting as broken.

## What your phone can confirm

`CHANNEL_CONFIRMABLE_ACTIONS` is a pinned frozen set — these four, for
every allowed user (only naming an admin widens it, for that person alone):

| action | what you're approving |
|---|---|
| `dispatch_run` | start a governed worker run |
| `scheduled_result_ack` | acknowledge a schedule's result |
| `read_url` | fetch one exact URL as text (v66) |
| `start_research` | a research run with its stated source allowlist (v66) |

Cards arrive as embeds with buttons (Discord) or inline keyboards /
Block Kit buttons (Telegram/Slack); ✅/❌ reactions work on Discord.
Timeouts DENY, never confirm (I6) — an unanswered card on your phone is
a card that did nothing.

## What only the admin can confirm from a phone

Shell allowlists, policy changes, patch landings, git/PR mutations, the
forge, moderation verbs, and any HIGH-risk card — **web UI** for everyone
except the channel admin. Without an admin, the Discord embed for such a
card carries its web-UI URL (v66) and the line that names an admin. The
admin's press is recorded as `channel:<channel>:<chat>:admin:<user>`, and
the server checks the stored admin list again on every confirm, so
`skep channel admin <channel> --clear` takes effect on the next press.
The trade (ADR 0067): a stolen admin messenger account can approve what the
admin can. Name an admin only on an account with its own second factor.
A fold too long to show whole on a phone stays web-UI-only for everyone.

## What comes to you unasked (v72-F3)

With a bound channel, the phone also receives pushes: run failures with
their reason, completed-but-unlanded patches ("land_run — landing IS how
skep commits"), pending approval gates, crashed runs with a
`resume_run` offer when a checkpoint survived, schedule auto-disables,
provider health transitions, scheduled notes/digests, and (opt-in)
completion lines. A morning-briefing `digest` schedule bound to your DM
is the intended daily loop.
