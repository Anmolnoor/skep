# ADR 0029 — Conversation-authored skills: observer + curator (v53-F1)

Date: 2026-07-17 · Status: accepted

## Context

Skills were learned from ONE source — repeated worker runs. A workflow
taught in chat evaporated when the chat ended; v51-F4's `create_skill`
requires someone to notice the pattern and write it down. The operator's
goal: "learn while performing things … so next time it saves time and
eventually uses fewer tokens."

## Decision

1. **The observer is opt-in, heuristic-only, and off the request path.**
   A settings toggle (`set_skill_observer`, carded, default OFF — the
   v47-F7 posture for ambient behavior). v1 detects one honest signal:
   a completed turn with ≥3 tool steps. No model call rides the sweep.
   It runs on the existing ticker with a message-id cursor — not as a
   post-turn hook inside the chat engine (deviation from the plan draft,
   recorded: the ticker is genuinely non-blocking and needs no generator
   surgery; a proposal arriving one tick later costs nothing).

2. **Drafts, never skills (ADR 0016).** The observer writes
   `SkillCandidate` rows, `provenance="conversation"`, status draft. A
   candidate never self-promotes.

3. **The human gate without the worker test.** `skill approve` admits a
   conversation draft directly — the v51-F4 reasoning extended: the test
   gate exists to prove a LEARNED WORKER recipe runs; a chat procedure
   has no runnable worker test, and the approve verdict IS the human
   gate. Learned drafts still must pass their test first (pinned).
   `promote_to_template` preserves `provenance="conversation"` so the
   registry records the generator.

4. **The curator surfaces, never acts.** Stale drafts (unreviewed >30d)
   are named in the digest. It never archives, merges, or deletes —
   `delete_skill` is a carded verb, and a tick silently unloading an
   approved skill would be a shadow path around that card (the v53
   review correction). Template IDLENESS surfacing is deferred with a
   named trigger: templates carry no usage tracking; when last-used
   tracking exists, the curator surfaces idle templates the same way.

## Consequences

- With the observer on, a repeated multi-step ask becomes a reviewable
  draft; approved, it appears in the chat skill index (ADR 0027) and the
  Queen follows the recipe instead of re-deriving it.
- Correction-detection and cross-chat repetition analysis are recorded
  future work, not shipped heuristics.
