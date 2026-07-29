# 0042 — Prompt schedules run read-only AND store-reads-only (v83-F5)

## Status

Accepted (planned v83; amended by the v83 plan review, item 4).

## Question

Hermes's `cronjob` runs a prompt at a time — the daily-briefing shape
skep lacked: every schedule caste either dispatched a worker or posted
static store-composed text. Can a scheduled Queen turn exist without
creating an unattended acting surface?

## Decision

A `prompt` schedule caste: at tick time the serve daemon runs the
schedule's instructions as a fresh Queen turn in the chat that created
the schedule (the v43-F6 binding). Two hard properties, both by
construction rather than by policy:

1. **Read-only.** The turn runs on the `/btw` machinery
   (`read_only=True`): mutations are refused, never carded. A card
   nobody is watching would expire denied anyway (ADR 0032); refusing
   up front means an unattended turn cannot even *queue* an action —
   the model never holds a trigger while the operator is away (I6).
2. **Store-reads-only.** The turn also refuses network-read tools
   (`search_web`; `read_url` is already a mutation and refuses under
   read-only). **The injection surface, named:** a recurring unattended
   turn that could fetch granted-domain pages would let an injected
   page steer every morning's briefing and chain further granted
   fetches — query strings are an egress channel, and the v83-F1
   budget raise makes ingested pages large. Scheduled turns therefore
   read the store (runs, approvals, chats, notes, memory) and nothing
   else; web reads belong in a live chat where the operator is present.

The CLI tick (`skep tick`) has no chat engine; a prompt tick there
fails honestly ("prompt schedules run inside the serve daemon") rather
than silently skipping (I8). The turn's transcript lands in the bound
chat like any conversation; the tick records `prompt_posted` /
`prompt_failed` in schedule health, and the reply pushes outbound
through the same channel funnel as note ticks.

## Consequences

- "Every morning summarize yesterday's runs and approvals" works
  end-to-end and can never act or fetch unattended.
- The blogwatcher-style skill cannot run unattended web fetches; it
  teaches a live-chat pattern (a scheduled *reminder* to run it live)
  until a reviewed-unattended-fetch design exists — a deliberate
  narrowing recorded here, not a bug.
- A future loosening (e.g. pre-listed URLs reviewed at
  propose_schedule card time) must amend this ADR explicitly.
