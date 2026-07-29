# ADR 0038 — Approvals announce themselves (v56-F5/F6/F7)

Date: 2026-07-18 · Status: accepted

## Context

A run hitting a gate transitions to `pending_approval` — and nothing
tells anyone. `notify_run_terminal` (the v47 terminal-notify path)
early-returned for it: only failed states and opt-in completions
notified. The status SSE turned the transition into a transient red
toast with no call to action; `get_run` guidance had no
pending_approval branch, so even a polling Queen said nothing; the
approvals view and Home fetched once per navigation while the nav
badge polled every 5s — badge and list disagreed; a card resolved on
another surface (second tab, deck, the v54-F1 auto-deny sweep — which
deliberately skips web SSE) left the first tab's buttons live and the
composer locked until reload; and the status stream snapshotted its
tracked runs ONCE at subscribe, so fast-gating runs were never
reported (also the v53-era approvals-test flake). Field words:
"sometimes approval don't hit the chat; stale somewhere."

## Decision

1. **A gate is a call to action, not an opt-in.** `notify_run_terminal`
   gains the `pending_approval` branch: a persisted transcript line in
   the dispatching chat + channel push, naming the reason and how to
   resolve (Approvals view / `/approve`). Same path failures already
   use; no new machinery.

2. **The Queen states it.** `get_run` guidance gains the
   pending_approval branch so a poll never shows a waiting gate
   without saying so.

3. **The badge poll refreshes what it counts.** The existing 5s poll
   re-renders the approvals/home views when the pending count changes,
   and re-renders the chat view when its pending cards drop to zero
   while the composer is locked. No new SSE channel; one poll, one
   truth.

4. **The status stream re-derives its tracked set every iteration.**
   Runs that went terminal before subscribe still emit one terminal
   event on first sight; runs dispatched after open join the set. The
   snapshot race — and the test flake it caused — die together.

## Consequences

- The chat is told when work stops to wait for a human, on every face
  (web transcript, messenger push, Queen guidance).
- Web views and the badge can disagree for at most one poll cycle.
- The status stream reports transitions it used to miss; its exit
  condition is a drained set plus a grace window, not a lucky race.
