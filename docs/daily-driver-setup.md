# Daily-driver setup: skep from your phone

The steps that make the daily flows work from Telegram: summarize a YouTube
video, start research, and get a 16:30 mail summary in a group. One step
each, CLI first. `skep doctor` names every step below that is still undone
and goes quiet once it is done:

```sh
skep doctor
skep channel status
```

`skep channel status` prints `can_confirm: on|off` for each configured channel.

## 1. Run the home on an always-on host

Schedules fire only while `skep serve` runs. A laptop that sleeps misses the
16:30 slot. Run the home on a machine that stays up.

## 2. Let the phone confirm cards

```
PUT /api/channels/telegram {"channel_can_confirm": true}
```

Or use the web UI's channel panel. There is no CLI for this step. With it on,
the phone confirms only the four channel-confirmable classes (dispatch,
scheduled-result acknowledgement, read_url, start_research). Every other card,
including every HIGH card, is still confirmed on the web.

## 3. Put the research chat on `operator`

A Telegram chat opens in the `assistant` role, which hides `start_research`.
Find the chat id in the web UI (or in `skep doctor`'s line), then:

```sh
skep role use operator --chat <chat id>
```

The risk, plainly: whoever holds that Telegram account gets the `operator`
role's tools in that chat. The phone still confirms only the four classes in
step 2. If you would rather not take that risk, skip this step and start
research from the web.

## 4. Promote the transcript tool and allow it

From a **web** chat, ask:

- `promote_tool yt-transcript`, then confirm the card on the web. This installs
  the shipped seed whose tool is `get_transcript`. The card shows the source
  digest and the one host the tool may reach (`*.youtube.com`); it reaches
  nothing else. An install from before Phase 07 stops running until you
  promote it again this way.
- `allow_mcp_tool server_id=forge-yt-transcript tool=get_transcript`, then
  confirm on the web. Every role asks on MCP calls (`mcp-gated`). Without this
  rule each transcript from the phone becomes a card.

Neither step has a CLI. If you promoted the tool before v122, your copy still
lists the old `yt_transcript` tool, which is not a read. Re-forge it from a web
chat: `suspend_tool yt-transcript`, then `promote_tool yt-transcript`. That
installs the current seed. Then add the allow rule above.

## 5. Make a Telegram group for scheduled delivery

A private chat with the bot is a single session (one skep chat). Make a
**group** with you and the bot, and add your Telegram user id to the
channel's `allowed_users`. The group becomes its own skep chat, and scheduled
summaries go there. With BotFather's group privacy mode on (the default),
the bot only sees commands, mentions and replies. Address it with
`@yourbot …`, or turn privacy off with `/setprivacy`.

## 6. Pull your mail at 16:25 (a script schedule, from a web chat)

Store the mail password once, at `~/.skep/supervisor/mail-secret` (mode 0600).
Script schedules run with a scrubbed environment: only `PATH`, `HOME`, `USER`
and a few locale variables are passed, and no `env_allowlist` applies. The
command therefore names the mail host and login inline. The password is
found through `HOME`. Never put `SKEP_HOME=<path>` inline: the Queen refuses
any command that sets it.

Find skep's own python. It is the interpreter on the first line of the
`skep` launcher (`head -1 "$(command -v skep)"`). Then, from a **web** chat (a
web chat has no channel binding, so the raw digest is never pushed to a
phone), ask for a script schedule:

> propose_schedule name=mail-pull caste=script every=1d start_at=2026-11-20T16:25:00-08:00
> instructions="SKEP_MAIL_IMAP_HOST=imap.example.com SKEP_MAIL_USER=you@example.com /abs/path/to/python -m skep.mcp_servers.mail --digest 5"

The digest prints the newest 5 messages as `From:` / `Subject:` / `Date:`
blocks with every body line quoted `> `, and stays under the 4000-character
script output cap. It only reads: no flag changes and nothing is marked read.
Confirm the card on the web. A script schedule is always a HIGH card.

## 7. Summarize it at 16:30 (a chained prompt schedule, from the group)

Send this exact sentence in the Telegram group (it starts with `@yourbot`,
your bot's username, because privacy mode drops group messages that do not
mention the bot):

> @yourbot Every day, start_at 2026-11-20T16:30:00-08:00, run a prompt schedule named
> mail-summary chained to mail-pull: summarize my new mail in five bullets and
> name each sender.

The card goes to the web, because a chained schedule is HIGH. Confirm it
there. Give an explicit UTC offset (`-08:00` here). A bare time is read as
UTC. The pull runs five minutes earlier because the chain reads mail-pull's
**last** output. A schedule keeps its time of day. "Run now" moves the slot
to the moment you pressed it. A daily schedule is a fixed 24 h interval, so
its local time shifts by an hour at daylight-saving changes: re-create it
then, or accept the shift.

## 8. Keep the research chat clean

Keep mail, notes and the group out of the research chat (the Telegram DM).
Once a chat has read both private and untrusted data, every card in it
becomes web-only. At the desk, `skep trust <chat id>` clears both bits:

```sh
skep trust <chat id>
```

## Desk turns reach the bound messenger

A chat bound to a Telegram, Slack or Discord conversation is one conversation
with two windows. The web UI shows everything. The phone also hears the
Queen's replies to turns started at the desk: a message typed in the web UI
(quoted as `(web) you: …`, cut to 300 characters), the continuation after you
confirm or deny a card on the web, and the follow-up of a card confirmed while
another turn ran. A card raised at the desk stays at the desk; it is re-sent,
with buttons where the phone may confirm it, the next time you write from the
phone. Attachments are not mirrored, and delivery is best-effort: the chat in
the web UI is the record.

The channel's `notification_level` is the dial: `all` mirrors desk turns,
`approvals` or `none` keeps them at the desk. A chat that has read private
files never sends a desk reply out; the phone gets one line saying a reply
stayed in the web UI. A conversation removed from `allowed_identities` stops
hearing desk turns. A group, or a Slack or Discord channel, has several readers, and all
of them see the mirrored replies, just as they already see the replies to
messages sent there. An unbound web chat is never pushed anywhere.
