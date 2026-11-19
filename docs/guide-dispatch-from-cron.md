# Guide: dispatch a run from cron or a script

<!-- Commands in this guide were run on skep 1.4.2 (Fedora, bubblewrap) on 2026-11-18
     with a throwaway SKEP_HOME and throwaway repositories under ~/.cache, with no API
     key and no engine login. Cron's environment was reproduced with
     `env -i HOME=... PATH=... SKEP_HOME=... skep ...`. Output is pasted as printed, with
     these rewrites: temporary repository paths became /home/you/code/..., the
     temporary home became /home/you/.skep. The wrapper in step 2 was run under
     `env -i` with the same shape (PATH line, `set -a` env file, `exec skep run`),
     with the test paths in place of ~/code/calc and ~/.config/skep/cron.env. A run of an external agent under cron
     with a real credential was not performed; what it prints is the same code path
     as guide-unattended-run.md, step 5. -->

This guide is for someone who already has a working run from a terminal
([`guide-unattended-run.md`](guide-unattended-run.md)) and wants the same run
to start on a timer or from another program. At the end you will have a script
that cron can call, you will know what its exit code means, and you will know
when to use skep's own schedules instead.

## The thing that goes wrong first

Cron does not run your shell's startup files. A job gets a short `PATH`
(often `/usr/bin:/bin`) and almost no other variables. Three things your
terminal had are missing:

1. `skep` itself, if pipx installed it under `~/.local/bin`.
2. The agent's CLI (`claude`, `codex`), usually also under your home.
3. The agent's credential (`CLAUDE_CODE_OAUTH_TOKEN`, `ANTHROPIC_API_KEY` or
   `OPENAI_API_KEY`), which you exported in your shell.

skep refuses each case before anything starts, with exit code 2. Both of these
were produced under a cron-like environment.

With the agent's CLI not on `PATH`:

```text
error: coding_engine 'claude_code' is not installed here ('claude' not on PATH) — `skep doctor` lists each engine; install it, or switch the project to another engine (`skep project setup --engine builtin`)
  next:     skep doctor  # engines, auth env, sandbox
```

With `PATH` fixed and no credential:

```text
error: coding_engine 'claude_code': NO AUTH ENV — set one of CLAUDE_CODE_OAUTH_TOKEN/ANTHROPIC_API_KEY in the environment the worker inherits (the daemon reads <home>/serve.env at startup; `skep run` uses your shell) — the line `skep doctor` prints for this engine
  next:     skep doctor  # engines, auth env, sandbox
```

And when `skep` itself is not on `PATH`, the shell answers before skep can
(exit code 127):

```text
/bin/sh: line 1: skep: command not found
```

Log the job's output (step 4) so you can see which one you have.

## What you need first

- The run works from your terminal, with the project already set up: engine,
  pinned verify command.
- The absolute paths of `skep` and of the agent's CLI: `command -v skep`,
  `command -v claude`.
- If you use a non-default home, its path. `--home` goes before the
  subcommand: `skep --home /path/to/home run …`, or set `SKEP_HOME`.

## 1. Put the credential in a file only you can read

```sh
mkdir -p ~/.config/skep
umask 077
printf 'CLAUDE_CODE_OAUTH_TOKEN=%s\n' 'paste-the-token-here' > ~/.config/skep/cron.env
chmod 600 ~/.config/skep/cron.env
```

Do not put the token in the crontab line: crontabs are often readable by
backups and shown in process listings.

## 2. Write the wrapper

```sh
#!/bin/sh
# ~/bin/skep-nightly.sh
set -eu

# cron has none of your shell's environment: say where things are.
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"

# the agent's credential, exported for the worker to inherit
set -a
. "$HOME/.config/skep/cron.env"
set +a

exec skep run "$HOME/code/calc" "make the failing test pass" \
  --execution-mode sandbox --quiet
```

```sh
chmod 700 ~/bin/skep-nightly.sh
```

`--quiet` drops the live progress lines and keeps the summary:

```text
task 01a1f134-77b5-7815-954f-11c0d8686bb1
  state:        completed (1.6s)
  verification: passed
  summary:      created hello.py and verified it runs.
  evidence:     /home/you/.skep/supervisor/audit/01a1f134-77b5-7815-954f-11c0d8686bb1
  next:         skep review 01a1f134-77b5-7815-954f-11c0d8686bb1
```

<!-- captured from a builtin-worker run with --quiet; the summary block is printed by
     src/skep/supervisor/cli_cmds.py:1014-1025 for every engine. -->

## 3. Test it the way cron will run it

Before trusting the timer, run the wrapper with an empty environment:

```sh
env -i HOME="$HOME" ~/bin/skep-nightly.sh; echo "exit $?"
```

If this works, cron will work. If it prints one of the errors above, fix
`PATH` or the env file. Run this way with an env file that held no credential,
the wrapper printed the `NO AUTH ENV` refusal and exited 2; pointed at a
project on the built-in worker it printed the summary block and exited 0.

## 4. Add the cron line

```cron
15 2 * * *  $HOME/bin/skep-nightly.sh >> $HOME/.cache/skep-nightly.log 2>&1
```

A run is not interactive under cron. If the worker stops to ask for a
permission, the run ends in state `pending_approval` with exit code 4 and
waits for `skep review <task_id> --approve`, `--deny` or `--allow-command`.
Nothing is approved on your behalf.

## 5. Use the exit code

| Exit | State | What a script should do |
|---|---|---|
| 0 | `completed` | the worker finished; **check re-verification before trusting it** |
| 2 | refused or a usage error | nothing ran; read the message (PATH, credential, verify command, sandbox) |
| 3 | `failed` | the worker gave up; the summary line says why |
| 4 | `pending_approval` | a human answer is needed: `skep review <task_id>` |
| 5 | `rejected` | policy or a human denied it |
| 6 | `worker_timeout` | the budget ran out; raise `--budget-wall-clock` or narrow the task |
| 7 | `worker_crashed` | the worker died; see `worker.log` in the evidence directory |

<!-- src/skep/supervisor/cli_cmds.py:69-76 and :1025. Exit 0, 2 and 3 were observed;
     4 to 7 are from the table in the code. -->

Exit 0 is not "the patch is good". In a run made for this guide the worker
reported `passed`, skep's re-run of the project's command failed, and the exit
code was still 0. The patch is held either way; to see which runs skep did not
confirm:

```sh
skep status --personal
```

```text
⚠ 2 completed run(s) NOT confirmed by re-verification (G10):
  skep review 01a1f131-762b-77f5-86a7-9cc9de4a8342  # worker claimed passed; supervisor re-run disagreed
  skep review 01a1f131-5eeb-7c0d-85ec-8a2239d4e924  # worker claimed passed; supervisor re-run disagreed
```

A scheduled run does not land anything unless the project has auto-landing
switched on ([`guide-auto-landing.md`](guide-auto-landing.md)). Otherwise each
run waits for your `skep review <task_id> --approve`.

## skep's own schedules: `skep schedule` and `skep tick`

skep can also keep the timetable itself. You add a schedule once, and a single
cron line calls `skep tick`, which dispatches whatever is due.

```sh
skep schedule add notes-hello ~/code/notes "Write a Python hello world script" --every 30s
```

```text
scheduled 'notes-hello': coding on /home/you/code/notes every 30s
  next run: 2026-11-19T00:26:39Z  (dispatch due work with: skep tick)
```

`--every` takes `30s`, `5m`, `2h` or `1d`. Thirty seconds was used here so the
schedule came due during the session; for a nightly run use `1d`.

```sh
skep schedule list
```

```text
name             caste    every   on    next run               last run               last outcome                             source
calc-nightly     coding   30s     yes   2026-11-19T00:27:39Z   2026-11-19T00:27:13Z   failed                                   inline
notes-hello      coding   30s     yes   2026-11-19T00:27:39Z   2026-11-19T00:27:13Z   completed                                inline
```

```sh
skep tick
```

```text
ran 'calc-nightly': 01a1f133-4dfc-7e38-9217-300c52e19efc (failed)
ran 'notes-hello': 01a1f133-5400-7f6f-b79c-120afe883acc (completed)
```

When nothing is due it prints `no schedules due`. `skep schedule health` shows
each schedule's failures and success rate, and `skep schedule remove NAME`
deletes one.

```cron
*/15 * * * *  $HOME/.local/bin/skep tick >> $HOME/.cache/skep-tick.log 2>&1
```

Three things to know before choosing this over the wrapper:

- **A scheduled run uses skep's built-in worker, not the project's pinned
  engine.** On 1.4.2, `calc` above pins `claude_code`, and the scheduled run
  was still handed to the built-in worker (`--worker-cmd` on `skep tick`
  changes which worker command that is). The project's verify command and
  landing policy do apply. To run Claude Code or Codex on a timer, use the
  wrapper with `skep run`, which honours the pin.
- **`skep tick` exits 0 whatever the runs did.** The states are in its output
  and in `skep schedule health`, not in the exit code.
- **Project setup creates a schedule.** `skep project setup` on a
  `trusted_local_dev` repository adds `<project>-maintain-weekly` unless you
  pass `--no-seed-schedules`. The first `skep tick` after it comes due will run
  it. `skep schedule list` shows what is there.

<!-- src/skep/supervisor/scheduler.py:958-972 passes no coding_engine to the dispatch;
     src/skep/supervisor/cli_cmds.py:2262-2281 is `skep tick`, which always returns 0. -->

`skep tick --auto-approve` is retired: it is still accepted, prints a note,
and lands nothing.

## What a running `skep serve` does by itself

<!-- from code, not run for this guide: src/skep/supervisor/serve/ticker.py:345
     (the same run_due as `skep tick`), src/skep/supervisor/serve/serve_cmds.py:50-75
     and :130-165 (serve.env). -->

If `skep serve` is running, you do not need a cron line for schedules: the
daemon dispatches due schedules itself, through the same code as `skep tick`
(and with the same built-in-worker limit).

The daemon has cron's problem too if a service manager starts it: it does not
have your shell's variables. For that it reads `<home>/serve.env` at startup,
one `KEY=VALUE` per line. A variable already set in its environment wins over
the file, and the startup banner prints the names it loaded, never the values:

```text
  serve.env: loaded CLAUDE_CODE_OAUTH_TOKEN
```

Keep that file at mode 600. It is read once, at startup: after changing it,
restart `skep serve`.

## What goes wrong, and how to tell

| What you see | Cause | Fix |
|---|---|---|
| nothing in the log, nothing ran | `skep` is not on cron's `PATH` | use the absolute path, or set `PATH` in the wrapper |
| `is not installed here ('claude' not on PATH)` | the agent's CLI is not on cron's `PATH` | add its directory to `PATH` in the wrapper |
| `NO AUTH ENV` | the credential is not exported in the job | steps 1 and 2; check with step 3 |
| exit 4 every night | the worker asks for a permission nobody is there to answer | approve it once with `skep review <task_id> --allow-command`, or widen the project policy |
| a schedule on a Claude Code project "fails" with `minimal coding worker only supports Python hello-world tasks` | scheduled runs use the built-in worker, and no model is configured for it | use the wrapper with `skep run` |
| runs you did not schedule | the seeded `<project>-maintain-weekly` schedule | `skep schedule remove <name>` |
| two runs of the same schedule | both `skep serve` and a cron `skep tick` are dispatching on one home | keep one of them |

<!-- The last row is a caution, not a reproduced failure: both paths call run_due on
     the same store. -->
