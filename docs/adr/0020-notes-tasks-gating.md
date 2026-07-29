# ADR 0020 — Notes & Tasks gating line (v7 Stage B)

Date: 2026-06-13 · Status: accepted

## Context

v6's chat rule was simple: reads run free, mutations become confirmation cards.
Notes & Tasks needs a sharper line. Carding every "note that..." makes the
feature unusable; auto-executing reminders or deletes would let the model change
machine behavior or destroy user data.

## Decision

Store Notes & Tasks in the supervisor's SQLite store, expose them over `/api`,
and split chat tools by consequence:

- **Free inside the turn:** `list_notes`, `list_tasks`, `add_note`, `add_task`,
  `complete_task`. These append or complete inert local state and write a
  durable `note_task_events` row with actor `chat-user`.
- **Confirmation-carded:** `set_task_due`, `delete_note`, `delete_task`. Due
  dates can drive future behavior, and deletes destroy user data, so they use
  the existing `chat_actions` verdict path and execute only as actor
  `chat-user` after confirmation.

The browser gets a first-class Notes & Tasks workspace over the same REST
routes. Due tasks are computed (`due_at <= now and status == todo`) instead of
binding to the scheduler in this stage.

## Consequences

- The model can capture useful local intent without making every note a modal
  approval flow.
- Anything that schedules behavior or destroys data stays behind the same gate
  as v6 supervisor mutations.
- Stage D can add channel notifications for due tasks without changing the
  stored task shape.
