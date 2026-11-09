---
name: skep
description: Install, onboard and operate skep, the local governor for AI coding agents — dispatch governed runs, read why a verdict was reached, approve landings, and never route around its policy. Use when a human asks you to set up skep, run or review work through it, or explain one of its decisions.
---

# skep

skep supervises coding agents. Every run works in a disposable git worktree
under a policy; skep re-runs the verification itself; the patch
reaches the repo only when a human approves it. The policy baseline is a
template the human locked by hash — nothing below it can loosen it.

Drive skep through its CLI. The CLI is the agent API: the verbs skep's own
chat (the Queen) calls are the commands you type.
State lives in `~/.skep`; `--home <dir>` (before the subcommand) points every
command at another home.

## Install and onboard

```sh
pipx install skep
skep onboard --template builder --yes --no-chat
skep onboard --check
```

- `--yes` stands in for the human's typed `yes` on the locked baseline. Use
  it only when the human asked you to set skep up.
- `--no-chat`: without it onboard starts `skep serve` and opens an
  interactive chat, which a non-interactive shell cannot drive.
- `--template builder` is the coding template. With no flag onboard picks
  `assistant`, the least-privilege one. Show the human
  `skep policy explain builder` if they have not chosen.
- With `--yes` onboard uses a local Ollama model. No Ollama: pass
  `--provider <preset> --model <name> --api-key-env <VAR>` (the variable's
  NAME, never the key), or `--provider mock` for a dry run.
- The work directory defaults to `~/workspace`; `--workdir <dir>` picks
  another. It is registered as a project named after the directory.
- To try it without touching the human's state:
  `skep --home <tmp> onboard --template builder --yes --no-chat --workdir <tmp>/work`.

`skep onboard --check` exits 0 when ready. It lists the baseline and its
hash, the sandbox rings that are live, and an egress-truth block (where model
calls go, which hosts workers may reach, `telemetry: none`). Onboarding is
resumable: run `skep onboard` again and it picks up at the first unfinished
step.

If an ACP client (T3 Code) will run its agents through skep:

```sh
skep acp install-hint
```

It prints the executable path and argument rows to paste into the client's
Local ACP command dialog.

## Explain a verdict

```sh
skep policy why ~/workspace shell run "git push"
skep policy explain baseline
```

`policy why <repo> <scope> <action> <value>` answers whether that action
would be allowed, with no dispatch. Read `operator.verdict` (`allow`, `deny`,
`require_approval` = a human answers a card) and `decided_by` /
`rule_id` (which layer decided: `floor/...`, the template rule, `role:<name>`).
`git push` above comes back `deny` from `floor.worker-git-hard-denies`.
Quote a value with spaces. Scopes include `shell`, `filesystem` and
`network`; `skep policy explain baseline` lists every rule.

`policy explain <subject>` prints a template, `baseline` or `role:<name>` as
an allow / ask / deny table; its rule ids are the ones `why` prints.
`skep policy baseline status` exits 1 if the locked baseline was tampered
with. `skep policy decisions` is the ledger of every decision, newest first.

## Verbs and faces

Every mutating verb has a chat face and a CLI (or REST) face that call the
same function, so they cannot disagree about what is allowed. A chat verb
becomes a confirm card for the human. A typed CLI verb is NOT carded — typing
it is the operator's decision. So type a mutating command only when the
human told you to.

| verb | chat tool | CLI |
|---|---|---|
| bind a repo to a policy | `setup_project` | `skep project setup <repo>` |
| dispatch a run | `dispatch_run` | `skep run <repo> "<instructions>"` |
| a team run (1-3 tasks) | `batch_dispatch` | `skep team --manager <role> --task <repo> "<instructions>"` |
| answer a blocked step | `allow_command_review` | `skep review <task_id> --allow-command` |
| land a completed run | `land_run` | `skep review <task_id> --approve` |
| refuse a run | `deny_review` | `skep review <task_id> --deny` |
| lock the baseline | `approve_policy_baseline` | `skep policy approve --template <name>` |
| switch role | `set_role` | `skep role use <name>` |
| push a non-default branch | `push_branch` | `skep branch push <repo> <name>` |
| open a pull request | `open_pr` | `skep pr open <repo> --branch <name>` |
| would this be allowed? | `policy_why` | `skep policy why <repo> <scope> <action> <value>` |
| show a policy as a table | `policy_explain` | `skep policy explain <subject>` |
| the decisions ledger | `policy_decisions` | `skep policy decisions` |

`skep review <task_id>` with no flag shows the patch and evidence.
`--approve` lands on a new branch `skep/<task_id>`, never the default branch.
Every verb and subverb takes `--help`; `docs/cli-reference.md` is the long
form.

## Roles and delegation

A role is a prompt, a voice and a policy layer under the baseline:
`builder`, `operator`, `assistant` (`skep role list`, `skep role show <name>`).
A role only tightens the baseline; the stricter verdict wins.
`skep run <repo> "<instructions>" --role <name>` gives a worker a role.
`--delegated-by <actor>@<role>` holds it to what that delegator holds: a
delegated worker gets the intersection, never more, and a rule the delegator
does not have counts as not granted. `skep policy why ... --role <name>
--delegated-by <actor>@<role>` answers the same question for a chain.

## Never

- Never push, pull, fetch, check out or switch branches for a worker, and
  never try to grant it. Those are floor denies no allowlist lifts.
- Never land work by hand. Do not apply a worker's diff, commit in its
  worktree or merge its branch outside skep. Landing is the approval:
  `skep review <task_id> --approve`, then `skep pr open` if wanted.
- No shadow permission systems. Do not wrap skep in your own allowlist,
  edit `~/.skep` by hand, or loosen policy outside its verbs. Widen policy
  only through `skep review <task_id> --allow-command`,
  `skep project setup` or `skep policy approve --reapprove`, so the ledger
  sees it.
- Never approve on the human's behalf unless they told you to. That covers
  `--approve`, `--allow-command`, `policy approve --yes` and `onboard --yes`.
- Tags and releases are the operator's. Never `git tag` or publish.

## Skep Development Patterns

<!-- TODO(operator): the ten Skep Development Patterns — not found in the
repo, plans/, .10x/ or the skills shelf when L2 was written; paste them here. -->

## More

`docs/agent-guide.md` covers explaining skep to a human. Also
`docs/cli-reference.md`, `docs/acp-governor.md` and `docs/policy-hook.md`
(governing Claude Code with `skep policy hook`).
