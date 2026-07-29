# ADR 0031 — Voice as a channel-layer capability (v53-F6)

Date: 2026-07-17 · Status: accepted

## Context

Skep had zero voice; the operator wants to TALK to it. v51 cut TTS as a
"channel-layer concern" — this un-rejects that cut with the scoped
rationale the cut itself implied: voice lives in the channel layer (web
UI + outbound delivery), never on the Queen's tool surface. The model
never sees or produces audio: STT feeds text in before the model,
TTS renders text out after the turn.

## Decision

1. **Web-first.** The browser's speech APIs cover "talk to skep" with
   zero backend: a mic button (recognition → composer text) and a
   spoken-replies toggle (synthesis over the reply). Honesty over
   mechanics: Chrome's recognition is CLOUD-BACKED (audio goes to
   Google's speech service) and the mic tooltip says exactly that —
   "the browser does all the work" would be a lie of omission.

2. **Server-side TTS is config-gated, default `none`, providers labeled
   by their egress.** `piper` is the LOCAL story. `edge` is Microsoft's
   cloud service — "free, no API key" does NOT mean no network: every
   rendered reply's text egresses when chosen (the draft plan claimed
   edge was local; the v53 review corrected it). `openai` likewise.
   The `set_tts_provider` card and the setting descriptions carry these
   labels; the tool result records the egress truth in the transcript.

3. **Config-gated, not operator-policy-gated — recorded boundary.**
   TTS/STT egress is channel infrastructure, the same trust class as the
   Discord API calls that deliver the text itself: enabled by explicit
   operator configuration, not per-call policy decisions. If voice ever
   grows per-content decisions (e.g. "never speak approval details"),
   that becomes an operator-policy scope.

4. **Delivery bounds.** Voice messages ride Discord (one multipart send
   on the existing messages endpoint), best-effort after the text lands
   — a render or upload failure is logged and swallowed (the outbound
   posture). Telegram/Slack delivery is demand-driven. Messenger
   voice-message STT is DEFERRED with a named trigger: it needs inbound
   attachment machinery per channel and no field test has ever sent one.

5. **No voice approval path.** Approvals remain text/card. A spoken
   "yes" is not a confirmation surface.

## Consequences

- `uv pip install 'skep[voice]'` pulls the optional providers; without
  them every path degrades to a clean logged skip.
- Reply text spoken aloud is capped (1,500 chars) — a message, not a
  podcast.
