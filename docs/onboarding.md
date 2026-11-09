# Onboarding: `skep onboard`

```sh
pipx install skep && skep onboard
```

`skep onboard` takes a new machine from nothing to a governed chat in five
steps. Plain prompts, no full-screen UI, so it works over SSH and inside
another tool's terminal. Every step writes a record under `~/.skep`, so
running it again picks up at the first step that is not done yet.

| Step | What it asks | What it writes |
|---|---|---|
| ① model | provider, model, endpoint, and the **name** of the env var that holds the API key (never the key itself). It looks for a local Ollama first, makes one test call, and prints where prompts go (`LOCAL … nothing leaves this machine` or `CLOUD … leaves this machine for <host>`) | the provider registry, the assistant settings, `profile.json` |
| ② work directory | a path (default `~/workspace`), created if missing | a project, with the `verify_command` inferred from the repo |
| ③ sandbox | `auto` (the native backend: Seatbelt on macOS, bubblewrap on Linux), `podman`, or `none`. Docker is detected but not offered, because skep has no docker backend | the `sandbox_backend` setting |
| ④ template | `builder`, `operator` or `assistant` (the default), shown as their `skep policy explain` tables | the locked baseline, after you type `yes` |
| ⑤ role | one of the same three names | `~/.skep/first_run.json` |

After step ⑤ it connects to `skep serve`, or starts it if it is not
running, and opens `skep chat`. Pass `--no-chat` to stop before that.

## `none` is a real answer

If the host has no usable sandbox, say `none`. The output then lists the
protections that are still active: the floor, the locked baseline and the
capability layer. Nothing confines the worker process, so the baseline is
locked with every allowed write turned into an ask. Under `builder`, a
worker asks before every write instead of writing freely inside the
workspace. You see the changed rules before you type `yes`.

## Scripts and docs

Every prompt has a flag, with the same names `skep setup` uses:

```sh
skep onboard --provider ollama --model qwen3:14b --workdir ~/code/app \
  --sandbox auto --template builder --role builder --yes --no-chat
```

`--yes` accepts the default for any step you did not pass a flag for and
skips the typed `yes`. `--provider` takes `mock`, `ollama`,
`openai-compatible`, or a preset id from `skep provider presets`. A flag
that differs from a step you already finished runs that step again. For
example, `--template operator` re-locks the baseline and shows the diff
first.

## `skep onboard --check`

This is `skep doctor` plus the onboarding checks. It also checks that:

- the baseline still matches its lock (a hand edit is reported as
  `baseline_tampered`)
- engine v2 is on for at least one scope
- the chosen sandbox backend is present
- the work directory is writable
- the model is reachable
- all five steps are done

It ends with the egress-truth block, which says where model prompts go,
which hosts the baseline lets workers reach, and ends with
`telemetry: none`. It exits 0 only when every check is ready.

## In the browser

The `/setup` page has a "First run (skep onboard)" card with the same five
steps. It calls the same functions through `GET /api/onboard` and
`POST /api/onboard/<step>`, so a step finished in the browser is finished in
the terminal too.
