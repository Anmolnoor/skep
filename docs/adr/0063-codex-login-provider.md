# ADR 0063 — the Queen on a ChatGPT plan: the Codex login as a provider credential

**Status:** accepted (v118)
**Related:** ADR 0051 (provider shelf; item 3, no borrowed identity, stands),
ADR 0062 (provider-key relay), ADR 0019 (the llm-secret exception to G2),
`docs/codex-chatgpt-login.md` (the codex engine's use of the same login).
Invariants touched: I5, I6, I8, I9, I11, I12.

## Context

The operator has a ChatGPT plan, signed in with the Codex CLI, and no
OpenAI API key. The codex *engine* can already run on that login
(ES-F120–F129). The Queen could not: every provider credential was an API
key. The plan's backend speaks the OpenAI Responses protocol skep already
has (`llm_responses.py`), so the gap is a credential source and two wire
details, not a new client.

Probed on the live backend (2026-11-16), `POST
https://chatgpt.com/backend-api/codex/responses` with only `Authorization:
Bearer <access_token from auth.json>`:

- the body skep builds today: 400 `{"detail":"Store must be set to false"}`;
  with `"store": false`: 200, `response.output_text.delta` events and
  `response.completed` with `usage.input_tokens`/`output_tokens`;
- `instructions` and one flat function tool work (the tool-call events the
  client already parses);
- no `chatgpt-account-id`, `originator` or Codex user-agent header is needed;
- `GET /models` is 400; `GET /models?client_version=0.169.0` is 200 with
  `{"models": [{"slug": ...}, ...]}`.

## Decision

1. **No refresh, no client id.** skep reads the access token from
   `$CODEX_HOME/auth.json` (default `~/.codex`) and uses it. It never calls
   OpenAI's token endpoint and never writes the file (why that matters is
   in `codex-chatgpt-login.md`). A token that is missing, not a ChatGPT
   sign-in, or within 5 minutes of expiry, and any 401 from chatgpt.com,
   make the Queen say so and name the fix: use `codex` once on this
   machine, or `codex login` (I9).
2. **No Codex-identifying headers.** The only concession is the model
   listing's `client_version` query value, a named constant
   (`CODEX_CLIENT_VERSION`) documented as a compatibility filter.
3. **The credential never falls through.** For a profile whose host is
   `chatgpt.com`, the key is the Codex login or nothing: never the
   profile's key file, never `llm-secret` (an ollama key must never be
   sent to chatgpt.com), never an env var. Keyed on the host, in the one
   resolver every caller uses (`resolve_provider_api_key`,
   `resolve_active_api_key`), and on the settings URL, so a manual config
   that points at chatgpt.com while another profile is active gets the
   login too. `set-key` and `provider login` refuse such a profile on the
   CLI and REST (chat has no set-key face).
4. **The Queen only.** A worker never receives the operator's token
   through this profile. Activating it does not rewrite `profile.json`
   (the workers' provider), so builtin workers, the reviewer caste and the
   ADR 0062 relay keep the provider and key they had. With nothing else
   configured a worker refuses before any key is read and names the way
   forward. Routing never picks the profile for a run, a run's
   `provider_hosts` never carry chatgpt.com, and the sandbox key carve-out
   (B125) unmasks no key file for it. `auth.json` stays masked (W33).
5. **Opt-in is the existing carded provider verb** (I6). The preset is
   `openai-codex` ("ChatGPT plan (Codex login)", `openai_responses`,
   `https://chatgpt.com/backend-api/codex`, default model
   `gpt-6.1-terra`, no key env, cost class `paid`). Its auth note, its
   egress line and the HIGH card on `add_provider` / `use_provider` /
   `set_assistant_model` say: uses your ChatGPT plan login (Codex); every
   Queen prompt leaves this machine for chatgpt.com.

The two wire differences live where the protocol does: `store: false` on a
Responses body whose base host is chatgpt.com (bodies for api.openai.com and
api.x.ai stay byte-identical, pinned in a test), and the listing's query
value and `models[].slug` shape.

## Consequences

- The Queen can run on the plan next to the ollama.com profile she runs on
  today; switching between them is `skep provider use`. Ollama behaviour
  and every other preset are unchanged.
- The config view, the provider list, `skep doctor` and the Settings key
  field report the login's presence, plan and expiry for this profile,
  never a value and never a misleading "key set" or "no key" (I8).
- While the Queen is on the plan, workers authenticate with the legacy
  `llm-secret` against their `profile.json` endpoint (the v19-F9 rule):
  a key held only in another profile's `llm-secret-<id>` file is not read
  for them. Saving that key as the assistant API key (Settings →
  assistant, which writes `llm-secret`) is the fix if it bites.
- The access token lives about ten days. skep does not extend it; the
  Codex CLI does, on its own schedule, the next time the operator uses it.
- The plan's backend is not a documented public API. If it changes, the
  Queen's turn fails with the provider's own words and the other profiles
  still work (I11: nothing else depends on it).

Invariants: **I5** (no new permission system: one resolver, one carded
verb), **I6** (the opt-in is a HIGH card the model cannot confirm),
**I8** (every view says which login and its state, never a value), **I9**
(every refusal names the fix), **I11** (the login is read in place, never
copied or refreshed), **I12** (no run's network pin gains chatgpt.com
through this profile; the egress list still names it).
