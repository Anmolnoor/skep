# ADR 0037 — Chat context: explicit window, budgeted replay, compaction (v56-F1/F2/F3)

Date: 2026-07-18 · Status: accepted

## Context

The Queen's turn loop resent the ENTIRE transcript every round — every
tool result as full verbatim JSON, forever — with no cap of any kind,
and never set `num_ctx`, so ollama truncated silently at its own small
default. skep's fixed floor (72 tool specs ≈ 38 KB + up to ~18 KB of
assembled system block) is ~14k tokens before one word of dialogue: on
a default window the model was blind-truncated from turn one, with no
error surfaced and a UI meter that measured none of it (it counted
thinking, which is never sent, against an arbitrary 24000-char divisor).
Field words: "fix the context on per chat."

## Decision

1. **The window is explicit.** `llm_num_ctx` (default 16384, floor
   1024) rides every ollama chat call as `options.num_ctx`;
   openai-compat servers manage their own window and ignore it.

2. **The store is the audit trail; the replay is a view.** Transcript
   rows are never truncated, edited, or deleted by context management.
   Only what is RESENT to the model each round is bounded: chars ≈
   tokens × 4, `budget = max(8000, window·4 − system block − tool
   specs − 8000 response reserve)`, measured — not estimated — at
   assembly time.

3. **Prior-turn tool results replay capped** (2000 chars + an honest
   marker naming the transcript); the current turn's tool results stay
   full — they are working data, not history.

4. **Overflow compacts deterministically.** Oldest replay overflow
   (never the newest 6 messages) folds into a per-chat digest
   (`chats.context_summary`, cursor `chats.compacted_through`): one
   condensed line per user/assistant message, tool bursts counted,
   digest capped at 4000 chars dropping its oldest lines. No model
   calls — compaction works offline and is testable byte-for-byte. The
   digest rides the system prompt as "Earlier in this conversation
   (compacted)".

5. **The meter is server truth.** Chat detail carries `context`
   (window, floor, history, budget, percent, compacted) computed from
   the same functions the replay uses; the composer renders it and
   stops guessing.

## Consequences

- Long chats keep the thread: newest turns verbatim, older turns as a
  digest, the model never silently loses the start of the conversation.
- A replayed tool result can be shorter than what the tool returned —
  the marker says so, and `get_chat_messages`/the transcript always
  hold the full data.
- The digest is lossy by design; if field tests show it loses the
  thread, an LLM-written summary can replace the line format behind the
  same columns (the trigger, not the default).
