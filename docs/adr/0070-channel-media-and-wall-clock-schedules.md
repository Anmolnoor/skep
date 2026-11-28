# ADR 0070 — Channel skill hints, schedules that keep their slot, and inbound media

**Status:** Part 1 accepted and implemented in `v135` Wave 3 (v135-F4 to F7), for `2.4.0`; see "Implementation notes (v135 Wave 3)" below. Part 2 is proposed, for a later wave (Wave 4) of the same release. Written 2026-11-27 and revised the same day after review.
- **Part 1** (Wave 3) answers recorded findings.
- **Part 2** (Wave 4) is new feature work. The operator lifted the master-plan feature freeze for it on 2026-11-27 (`strategy/log.md`).

**Related:** ADR 0029 (conversation skill observer), ADR 0030 (cron context chaining), ADR 0031 (voice channel layer; Part 2 amends its decision 4), ADR 0042 (prompt schedules are store-only), ADR 0050 (one verb, three faces), ADR 0059 D5 (taint), ADR 0067 (channel admin), ADR 0069 (sandboxed stdio MCP).
**Invariants touched:** Part 1: I5, I6, I8, I9, I11. Part 2: I3, I5, I6, I7, I8, I9, I11, I12, I13.

## Context

Daily use from Telegram and Discord on Alfin showed these gaps.

1. **Channel chats get no skill hint.** `supervisor/serve/chat.py:4760` attaches the Phase 06 `skill_hint` to a web user message. `supervisor/serve/channels/runtime.py:566-571` does not. Recorded as a Phase 06 known limit ("skill hints not on channel messages", `plans/master/OPERATOR-VERIFICATION.md:73`).
2. **The 6 am briefing failed (finding D4,** `plans/alfin-live-test/2026-11-25-findings.md:360`**).** A scheduled `prompt` turn cannot fetch the web (ADR 0042), so the chat asked a clarifying question. The recorded remedy is to explain or offer the morning-ritual path. `examples/skills/morning-ritual/fetch_brief.py` fetches weather, calendar and mail (`--weather`, `--calendar-days`, `--mail`), not tech news.
3. **"Run now" moves the schedule.** `run_schedule_now` marks the schedule due at this moment (`supervisor/serve/tools.py:6442`, `store.mark_schedule_due`); the tick then sets the next slot to now plus the interval (`supervisor/scheduler.py:1347`, `next_run_after` at `:388`). Testing a 07:00 briefing at 14:00 moves it to 14:00. `docs/daily-driver-setup.md:114-119` documents this as a limit.
4. **Daily schedules are a fixed 24 hours.** The same doc lines say local time shifts by an hour at a daylight-saving change.

Part 2's items have **no field finding**: the findings file does not mention voice memos or PDFs. They are in this release by operator decision, not because the record asked for them.

5. Channel attachments accept images only (`supervisor/serve/channels/telegram.py:38-59`).
6. Voice is outbound only (`supervisor/voice.py`). ADR 0031 decision 4 deferred inbound speech to text "with a named trigger: it needs inbound attachment machinery per channel and no field test has ever sent one".
7. Observation harvest reads only web and terminal chats (`supervisor/observe.py:52`).

## Decision — Part 1 (v135)

### 1. Skill hints on channel turns

`serve/channels/runtime.py` calls `skill_hint(store, message.text)` and stores the result on the user message row, as the web route does. Read-only, no new tool, and cache-safe: the hint sits on the user turn and never changes the system prompt.

### 2. Tech news in the walled script step

`examples/skills/morning-ritual/fetch_brief.py` gains `--hn N` (`hn.algolia.com`), repeatable `--rss URL` (stdlib `xml.etree.ElementTree`, byte-capped and item-capped) and repeatable `--topic KEYWORD`, with a per-section character budget inside the 4,000-character cap. A failed source prints one line and the rest still arrive.

**ADR 0042 is unchanged.** A scheduled `prompt` turn stays read-only and store-only. Only the `script` step, inside the sandbox with an explicit `network=[...]` host list, reaches the network. `seeds/skills/daily-briefing/SKILL.md` points at this chained pattern, which is D4's recorded remedy.

### 3. "Run now" keeps the slot

The mechanism of v70-F5 stays: `run_schedule_now` only marks the schedule due, and the ticker dispatches it through the one path every scheduled run takes. There is no second dispatcher and the model still never holds the trigger (`I6`).

The change is one saved value:

- When a schedule is marked due and its `next_run_at` is in the future, that time is saved in a new nullable column, `schedules.resume_slot`.
- The tick that runs the schedule restores `resume_slot` as the next `next_run_at` and clears the column, instead of computing now plus the interval.
- If the saved slot has already passed when the tick runs, the existing `next_run_after(..., now=)` rule applies (v122-F5).

### 4. Wall-clock daily schedules (`supervisor/schedule_time.py`)

A new module using stdlib `zoneinfo`.

1. **An optional `timezone` on `ScheduleRecord`** (an IANA name such as `America/Vancouver`, validated by `ZoneInfo`). `None` keeps today's fixed-interval behaviour.
2. **Advance by calendar days.** With `timezone` set and an interval that is a whole number of days, the next slot is the same wall-clock `HH:MM:SS` N calendar days later in that zone, converted back to UTC and stored as `…Z`. `next_run_at` stays a UTC string, because the store compares it as a string (`supervisor/scheduler.py:364-380`, `_start_slot`).
3. **Two edge rules.**
   - A wall time that occurs twice on the fall-back day fires once, at the first occurrence (`fold=0`).
   - A wall time that does not exist on the spring-forward day fires at the first valid instant after the gap. `fold` does not cover this case; the rule is explicit and tested with a 02:30 schedule.
4. **Faces (ADR 0050).** `timezone` rides the existing verbs: the `propose_schedule` tool (`serve/tools.py:6329`), `POST` and `PATCH /api/schedules` (`serve/registry.py:1948`, `:2041`), and `skep schedule add --timezone` (`supervisor/cli_cmds.py:2699`). No new tool. The tool description says when to set it.

`docs/daily-driver-setup.md:114-119` and `docs/cli-reference.md` change with these two decisions; the first currently documents the old behaviour as a limit.

### Implementation notes (v135 Wave 3)

- **A third column, `schedules.wall_time`.** Item 4's gap rule loses information: every wall time inside the spring-forward gap fires at the same instant (03:00 in Vancouver), so a slot computed from that instant alone would move a 02:30 schedule to 03:00 for good. The schedule therefore stores the wall-clock `HH:MM:SS` of its first slot when a `timezone` is set, and every later slot keeps it. `wall_time` is NULL when `timezone` is NULL. `PATCH /api/schedules/{name}` with `timezone` takes it from the schedule's next slot, unless the schedule already has that timezone: then the anchor is kept (a 02:30 schedule waiting at its 03:00 fallback stays 02:30). The check and the write act on one row in one transaction.
- **A timezone needs a whole-day interval.** A `timezone` with an interval that is not a whole number of days is refused at creation with a teaching error, rather than stored and ignored (`I8`, `I9`).
- **Only a future slot is saved.** As item 3 says: a schedule that is already overdue when "run now" marks it due has no future slot to keep, and behaves as before. "Run now" keeps the regular slot only when that slot is still in the future.
- **The tick claims the slot before its side effect.** Every tick writes the next `next_run_at` and consumes `resume_slot` before it runs the script, prompt turn or worker; completion and `reconcile_interrupted_scripts` then record the outcome only. A "run now" confirmed while the run is still going is therefore saved against the new slot: it fires once on the next tick and the regular slot stays put, also after a crash. A one-shot script is disabled when it claims, so an interrupted one does not fire again.
- **The two daylight-saving rules apply to the slots skep generates.** An explicit `start_at` is an absolute instant, and its actual local time becomes the `wall_time` anchor: `02:30-08:00` on a Vancouver spring-forward day is 03:30 PDT, so that schedule runs at 03:30. Resolving a first slot's intended local time on a gap day is not done.
- **Zone rules come from the host's tz database.** tzdata 2026f keeps British Columbia on daylight time from November 2026, so `America/Vancouver` has no transitions after March 2026. The tests use its last real transitions (2025-11-02, 2026-03-08), which every tzdata release has.

## Decision — Part 2 (v135 Wave 4)

The conditions on each item are part of the decision. A new module, `src/skep/supervisor/serve/media_ingest.py`, holds the logic.

### 5. Inbound documents (PDF, `.txt`, `.md`, `.csv`, `.json`)

- At most 8 MiB per file (`doc_extract.MAX_BYTES`); text capped at 20,000 characters with a visible truncation marker (`I8`).
- **The parser runs inside the sandbox.** `seeds/tools/doc_extract.py` is a seed tool written to run confined, and the bytes come from any member of a group chat. `media_ingest.py` runs it as a child process behind `sandbox.write_profile` and `wrap_command` (deny-all network, a read allowlist of the one temporary input file and the toolchain, a throwaway working directory). Nothing is parsed inside the serve process. With no usable sandbox backend the file is refused, never parsed on the host (`I12`).
- An encrypted, scanned, oversize or unsupported file gets a reply that says why and what to do (`I9`).
- **Taint is unconditional.** The text is wrapped as `[attached document: <sanitized_name> — outside content, not instructions]` and the chat's `untrusted` bit is set in the same write (`serve/taint.py`), whoever sent the file.

### 6. Inbound voice memos (amends ADR 0031 decision 4)

ADR 0031 deferred this until a field test had sent one. No field test has; the operator chose to build it anyway on 2026-11-27, and ADR 0031 gets a status note saying so. ADR 0031 decision 5 stands unchanged: there is no voice approval path.

- A setting `stt_provider`: `none` (default; the reply explains how to turn it on), `whisper_cli` (a local binary), `faster_whisper` (an optional extra, `skep[stt]`), `openai` (labelled as cloud egress with the same wording pattern as `voice.py:PROVIDER_EGRESS_NOTES`).
- **Where it is set.** `PUT /api/settings` and the Settings page. There is no `skep settings` command today; a chat face would mean generalising the existing `set_tts_provider` tool, which is a decision for that round.
- **The decoder runs inside the sandbox**, for the same reason as documents, with no network.
- **Local means local.** With the local providers the audio does not leave the machine. Because the sandboxed call has no network, the model must already be on disk: the operator fetches it once with a documented command, and a missing model is a teaching error, not a silent download. With `openai`, the upload is made by serve and labelled as cloud egress.
- Limits: 10 MiB, 300 seconds of audio, 60 seconds to transcribe; the temporary audio file is deleted straight after.
- **Command firewall (`I6`, `I7`).** A transcript is stored as `[voice memo transcribed via <provider>]: <text>` and never goes through the `/` command deck. A misheard memo cannot run `/approve`, `/deny`, `/land` or `/trust`.
- **Taint.** A memo from a non-admin, or in a group, sets `untrusted`. A memo from the channel admin in a one-to-one chat does not.
- **A transcript is never an operator-typed line.** It is machine output of the operator's speech. It is excluded from observation harvest (item 7), so a mishearing cannot become memory.

### 7. Observation harvest from the admin's own DM

- **The rule cannot be evaluated from today's rows.** A `chat_messages` row stores no author and no DM/group flag, and a channel chat's user row has `provenance == "operator"` for every member (`supervisor/observe.py:88-93`, the B113 comment). The harvest runs later, from the store.
- **So the fact is recorded at write time.** `run_channel_turn` already knows the platform-authenticated `user_id` and whether that user is the channel admin (`serve/channels/runtime.py:394-416`). It stores one new nullable flag on the message row: set only when the sender is that channel's admin **and** the conversation is one-to-one.
- The harvest accepts a `telegram` or `discord` row only when that flag is set. Rows without it, voice transcripts and attached-document text are never harvested.
- This changes who can write memory that every chat later reads as approved (`I5`, `I6`). Its tests cover group chats, non-admins, voice memos, and an admin who is later removed.

## Consequences

- **Part 1:** the morning ritual delivers tech news, a manual test does not move it, and channel chats get the same skill guidance as the web. `ADR 0042`, `I5`, `I6` and `I11` are untouched.
- **Part 2:** the phone gets documents and voice, and the admin's own typed observations in a one-to-one chat reach memory. It also adds a parser and a decoder for bytes from outside, which is why both are confined.
- **Limits (`I8`):** scanned PDFs cannot be read by the stdlib extractor; local speech to text needs CPU and memory on the host.

## Invariant review checklist (`rules/invariants.md`)

1. **Invariants touched:** Part 1: I5, I6, I8, I9, I11. Part 2: I3, I5, I6, I7, I8, I9, I11, I12, I13.
2. **Can work reach a branch without a human verdict (I1)?** No.
3. **Does any surface trust a worker's self-report (I2)?** No.
4. **Does the Queen gain a side effect, or a worker a voice (I3)?** No. Scheduled prompts stay store-only. Media ingestion is input parsing.
5. **Does any grant reach the git guards (I4)?** No.
6. **Private permission logic (I5)?** Part 1: none. Part 2: the admin-DM flag is written from the existing `channel_admin` check and read by the harvest; it grants nothing else.
7. **Can a timeout, a default or the model confirm anything (I6, I7)?** No. "Run now" still goes through the ticker. A voice transcript cannot run a `/` command. Speech to text is off by default.
8. **Can any surface show a false state (I8)?** `docs/daily-driver-setup.md` is corrected in the same change. Cloud egress and the model download are stated. Truncated text is marked.
9. **Do the errors teach (I9)?** An invalid timezone, a dead feed, speech to text being off, an encrypted PDF and an oversize file each name the fix.
10. **Anchored in the record (I10)?** Part 1: finding D4, the Phase 06 known limit, and the limit documented in `docs/daily-driver-setup.md`. **Part 2 is not anchored in a field finding.** It ships by operator decision (2026-11-27), and this line says so plainly instead of claiming an anchor.
