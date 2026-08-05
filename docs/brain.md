# The brain dial — choosing the assistant model

The Queen's model is a setting, not an identity. skep ships tuned for a
small local-friendly model (glm-5.2 via ollama.com), and the recorded
model-quality limits (v48 personality adherence, v50 re-asking, v70 loop
lessons) are properties of that choice — turn the dial and they move.

## The five protocols

| protocol | endpoint shape | examples |
|---|---|---|
| `ollama` | native Ollama API | local daemon (`http://localhost:11434`), ollama.com |
| `openai-compat` | `/v1/chat/completions` | OpenRouter, DeepSeek, any OpenAI-style server |
| `anthropic` | `/v1/messages` (v72-F1) | `https://api.anthropic.com`, MiniMax, Kimi For Coding |
| `openai-responses` | `/v1/responses` (v108-F5) | OpenAI's Responses API, xAI |
| `bedrock` | Converse + SigV4 (v108-F6) | `bedrock-runtime.<region>.amazonaws.com`, AWS env creds |

Most named providers need no protocol thinking at all: `skep provider
presets` lists the built-in catalog (v108 — OpenRouter, DeepSeek, GLM,
Kimi, MiniMax, Copilot, Bedrock and ~25 more) and `skep provider add
<id> --preset <preset> --activate` registers + switches in one step.
Each profile can hold its own key (`skep provider set-key`, a 0600
`llm-secret-<id>` file); the GitHub Copilot preset exchanges your own
GitHub token for its short-lived bearer automatically (v108-F7).

## How to switch

- **From chat:** ask for it — the Queen proposes `set_assistant_model`
  and a confirmation card shows exactly what changes. Scope `default`
  switches the saved config; scope `chat` overrides one chat only
  (`model: default` clears it). The card never carries an API key.
- **From the UI:** Settings → assistant (base URL, model, protocol,
  key). The key lives in a 0600 file (`supervisor/llm-secret`), never
  in SQLite, never in any GET response.
- **From the CLI:** `skep setup --provider anthropic --model
  claude-sonnet-5 --endpoint https://api.anthropic.com
  --api-key-env ANTHROPIC_API_KEY`.

## What inherits the change

The saved assistant config is the shared source of truth: the default
coding worker (and the named Ollama worker) bootstrap from it whenever no
explicit worker `profile.json` is present. Switching the Queen to a
frontier model upgrades those workers on their next run — one dial, both
brains. Per-chat overrides (`scope: chat`) touch nothing but that chat.

## Honest footnotes

- `num_ctx` is **ollama-only**. openai-compat and anthropic servers size
  their own context window; the setting is ignored there (the chat
  replay budget still applies — it derives from `num_ctx` either way).
- The anthropic branch translates tool calls at the client boundary;
  history carries no provider-specific ids, so a mid-conversation
  protocol switch is safe.
- A cloud endpoint means chat text leaves this machine (the same truth
  the TTS tooltips state). Local-first stays available: any model your
  Ollama daemon can hold.
