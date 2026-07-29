# ADR 0019 — LLM chat in the Queen: the voice, and the gated hands (v6)

Date: 2026-06-12 · Status: accepted

## Context

v5 deliberately parked "LLM-in-the-Queen chat" (non-goal C2): a second model
and key are a real new trust surface. v6 un-parks it. The operator wants to
*talk* to skep — Ollama Cloud first — and wants the model able to help drive
the supervisor (runs, approvals, policy) without ever weakening the human
gates. The v6 RFC chose an in-daemon chat proxy over chat-as-worker-runs
(a worktree + subprocess per message; seconds of latency, no streaming) and
over a sidecar service (a second lifecycle for a single operator).

## Decision

**A small LLM client inside `skep serve`, a durable chat transcript in the
store, and tools split into two tiers: reads run free, mutations only ever
*propose*.** Zero contract change — the worker boundary does not move.

### 1. The Queen's model is not the worker's model

Chat config (`llm_base_url`, `llm_default_model`, `llm_protocol`) lives in the
v5 settings table, fully separate from the worker's `profile.json` (A6). The
default protocol is native Ollama, covering Ollama Cloud
(`https://ollama.com` with a bearer key) and a local daemon
(`http://localhost:11434`, no key). v7 Stage A adds an OpenAI-compatible
adapter (`openai-compat`) for LM Studio, vLLM, OpenRouter-style servers, and
similar APIs.

The protocol difference stops inside `serve/llm.py`: `/api/tags` and Ollama
NDJSON chat chunks are normalized with `/v1/models` and `/v1/chat/completions`
SSE chunks. OpenAI-compatible streamed tool-call argument deltas are accumulated
and parsed back to the same dict argument shape chat already uses. `POST
/api/llm/test` probes with optional pre-save overrides — the UI flow is
protocol + URL + key → test → live model list → pick a default.

### 2. The one deliberate G2 exception

skep's posture is "store the env-var *name*, never a secret" — but pasting a
key in the UI requires storing it. Resolution: a `llm-secret` file beside the
serve token, mode 0600, on the same data volume. Never in SQLite (it would
ride DB backups), never in any GET (responses say `api_key_set: true`), and
the `SKEP_LLM_API_KEY` env var wins when set. Worker-side G2 is unchanged.

### 3. A durable transcript, streamed over fetch

`chats` / `chat_messages` tables on the single-writer store; every turn
persists before its stream closes, so a refresh replays the conversation.
Replies stream as SSE read from a `fetch` POST — fetch can set the token
header, so the cookie dance exists only where the server pushes unprompted
(EventSource on run events). A half-reply that dies upstream is kept and the
client told via an `error` event.

### 4. The hands are gated: the model never holds the trigger

Read tools (runs, approvals, policy, templates, skills, schedules, repos)
execute inside the turn — the model may always look. Mutating tools
(`set_policy`, `approve_review`, `deny_review`, `dispatch_run`) NEVER execute
from the model's hand: each call pauses the turn into a `chat_actions` row
and a confirm-card in the chat. The verdict endpoints execute (or refuse) and
stream the model's continuation; a denied action is reported to the model as
denied. New messages are refused while a card is open, and a turn is capped
at 8 tool rounds.

### 5. One implementation of every verb

`actions.py` extracts the supervisor verbs (apply patch Q5, resume past gate
Q8, submit run, update policy) from the HTTP handlers' closures. A
chat-confirmed action and a button in the Approvals view run the *same*
function into the *same* audit trail — chat actions land as actor
`chat-user`. The chat layer cannot reach around the gates because it calls
the layer that owns them.

## Consequences

- The model reads supervisor state; treat chat history as sensitive to the
  same degree as the audit trail it can quote.
- A new runtime dep (httpx) and scripted fake-Ollama/fake-OpenAI test doubles —
  chat logic is tested over real localhost HTTP, like the fake worker.
- Tool results are stored in the transcript, so the *evidence* a verdict was
  based on is replayable later.
- Deep Research and channel/mobile fronts remain recorded roadmap items — not
  silently dropped.
- Proven in tests (suite at 196, 17 of them new: LLM config/client, chat
  sessions, gated tools) and in a real browser: configure →
  test → pick model → chat → model proposes `set_policy` → card → approve →
  policy actually flips → model continues with the result.
