# Guide: let confirmed runs land by themselves

<!-- Every command and transcript in this guide was run on skep 1.4.2 (Fedora,
     bubblewrap) on 2026-11-18 with a throwaway SKEP_HOME and throwaway repositories
     under ~/.cache, using the built-in worker with no model configured (it can only
     write hello.py). Output is pasted as printed, with these rewrites: temporary
     repository paths became /home/you/code/..., the temporary home became
     /home/you/.skep. Lines left out are marked "...". The refusals added after 1.4.2 (a
     known-vacuous pin, a patch touching the test setup) are from code. -->

By default a finished run waits for you: nothing is applied to your repository
until you run `skep review <task_id> --approve`. This guide is for someone who
wants some runs to land without that step. At the end you will have a project
where a patch that skep itself confirmed is committed to a `skep/*` branch with
no approval from you, and you will know what still stops it.

What does not change: your default branch never moves by itself. Auto-landing
only ever writes a `skep/...` branch. Merging it is still yours.

## What you need first

- skep 1.4.2 or newer and a usable sandbox (`skep doctor` shows
  `sandbox: ready`).
- A project with a **pinned verify command**
  ([`guide-pin-verify-command.md`](guide-pin-verify-command.md)). Without it
  the main lane does not fire.
- For the `--unlocked` switch only: an approved policy baseline
  (`skep policy approve --template builder`, or `skep onboard`).

## The lane

| Lane | Lands | Requires |
|---|---|---|
| `verified-patch` | any diff | the worker reported `passed`; skep's re-run of the **pinned** verify command passed; no risk flags |

`--auto-approve` used to switch on a second, dependency-only lane
(`deps-safe`). It is retired: `skep run`, `skep tick` and `skep serve` still
accept it for one release, print a note, and land nothing because of it.

<!-- src/skep/supervisor/policy.py:44-76 (the two rules), :100-126 (why a rule does not
     apply), src/skep/supervisor/dispatch.py:1073-1102 (which rules a run gets). -->

The lane records an approval whose actor is `auto:verified-patch`, so the
record always says a rule approved it and which one.

## Switching on `verified-patch`

There are three ways to switch it on. All three are per project.

### a. The maintain phase

```sh
skep project setup ~/code/notes --phase maintain
skep project show notes
```

```text
project notes
  name:      notes
  strategy:  trusted_local_dev
  phase:     maintain
  ...
      "auto_apply_branch": "skep/maintain",
      "auto_apply_verified_patch": true,
      ...
      "verify_command": "pytest"
    }
```

In the maintain phase confirmed patches accumulate on one branch,
`skep/maintain`, instead of a new branch per run.

Use `skep project setup <repo> --phase <phase>` to change phase, as above.
`skep project set-phase <id> <phase>` also changes it, but on 1.4.2 it dropped
the project's `coding_engine` and `allowed_shell_commands` when this guide was
written; if you use it, check `skep project show <id>` afterwards.

Now a run:

```sh
skep run ~/code/notes "Write a Python hello world script" --execution-mode sandbox
```

```text
dispatching task against /home/you/code/notes
  → worker started (vcoding-minimal-0.1.0)  project: notes (trusted_local_dev/maintain)  dispatch: allow dispatch.allow.run_request_resolved  landing: allow landing.auto_apply.project_policy_enabled
  ...
  → verification: passed
  → terminal: completed
  → re-verify: passed [confirmed]  worker passed  re-ran clean: all exit 0 [command from the project's pinned verify_command]  re-ran pytest  -> exit 0
  → approval needed: apply_patch  project: notes (trusted_local_dev/maintain)  rule 'verified-patch' fired: verification=passed, re-verified=True, risk_flags=[], 1 file(s) within scope ['*']  policy: allow landing.auto_apply.project_policy_enabled
  → approval resolved: apply_patch approved by auto:verified-patch skep/maintain  project: notes (trusted_local_dev/maintain)  policy: allow landing.auto_apply.project_policy_enabled  rule 'verified-patch' fired: verification=passed, re-verified=True, risk_flags=[], 1 file(s) within scope ['*']
task 01a1f131-c342-7b19-9888-7f168a815bdc
  state:        completed (1.1s)
```

The `landing:` field on the first line tells you before the worker starts
whether this run can auto-land. Afterwards:

```sh
git -C ~/code/notes branch --list
git -C ~/code/notes log --oneline skep/maintain
```

```text
* main
  skep/01a1f131-513d-7650-873b-16247605c45b
  skep/maintain
```

(The first `skep/...` branch is an earlier run that was approved by hand.)

```text
678bb37 Apply skep task 01a1f131-c342-7b19-9888-7f168a815bdc
7501a3d baseline
```

`main` is untouched. `skep review <task_id>` shows who approved:

```text
  approval:     approved (apply_patch) by auto:verified-patch at 2026-11-19T00:25:33Z
```

Once `skep/maintain` exists, the next run in this project starts from it, so
each run sees what the earlier ones landed.

### b. `--unlocked`

```sh
skep project setup ~/code/notes --unlocked
```

On a home with no approved baseline this is refused:

```text
error: unlocked=true needs an approved policy baseline (this home: no_baseline); every worker ask would still be the baseline's. Approve one first (`skep policy approve`).
```

It also needs a pinned verify command and a usable sandbox. Once saved,
`"unlocked": true` appears in `skep project show`, and a run lands on its own
`skep/<task_id>` branch:

```text
  → worker started (vcoding-minimal-0.1.0)  project: notes (trusted_local_dev/build)  dispatch: allow dispatch.auto_allowed.unlocked  landing: allow landing.auto_apply.project_policy_enabled
  ...
  → approval resolved: apply_patch approved by auto:verified-patch skep/01a1f132-66f3-7b1f-a4f9-ee073dd8e651  ...
```

`--unlocked` is more than auto-landing: it also lets runs that the chat
assistant dispatches start without a confirmation, and lets a sandboxed
worker's routine command questions answer themselves. Denies and the hard
floor still hold. Turn it off with `--no-unlocked`. Read
[`adr/0061-unlocked-mode.md`](adr/0061-unlocked-mode.md) before using
it on a repository you care about.

### c. The policy key

`auto_apply_verified_patch: true` in the project's policy is the switch
itself. The maintain phase sets it as a phase default, and `--unlocked` implies
it when the policy is resolved (which is why `skep project show` can print
`"auto_apply_verified_patch": false` next to `"unlocked": true` and the run
still lands).

## What auto-landing still refuses

Each of these leaves the run `completed` and waiting for
`skep review <task_id> --approve`, with the reason recorded on the run.

| Refused | The recorded reason |
|---|---|
| The default branch | never a target: `auto_apply_branch` must be `skep/<name>`, otherwise the branch is `skep/<task_id>` |
| A patch skep did not confirm | `supervisor re-verification did not confirm the result` |
| A worker that did not report `passed` | `verification is '<outcome>', not 'passed'` |
| `verified-patch` on an unpinned project | `the project pins no verify_command, so re-verification re-ran the command the worker chose for itself — set verify_command in the project policy to auto-land (v88-F4); the patch is still landable by hand` |
| A run with risk flags | `risk flags present: [...]` |
| A pin that is known-vacuous (`true`, `git diff --check`, ...), or a patch that touches the test setup or a file the pin runs | named in the run's `auto_apply_blocked` record; the full list is in [`verification.md`](verification.md#current-limits) |
| A run that changed nothing | `no changed files to apply` |
| A branch that moved past the commit skep verified | `<branch> is at <sha>, past the commit re-verified (<sha>); land it by hand` |

<!-- Reproduced on 2026-11-18: the unpinned-project row (a maintain-phase project with
     no verify command) and "no changed files to apply".
     From code, not reproduced: the other rows, src/skep/supervisor/policy.py:100-126
     and :213-233; the skep/<name> rule, src/skep/supervisor/projects.py:170-190. -->

A patch that matches a rule but cannot be applied is recorded as denied with
the failure, not left pending.

The recorded reason is in the run's state history. `skep review` does not
print it on 1.4.2; the quickest check is the `landing:` field and whether an
`approval:` line by `auto:...` is there.

## How to tell what happened

```sh
skep status --personal
```

Each row has an `autonomy` column: `l:allow auto_apply.project_policy_enabled`
means the run was allowed to auto-land, `l:require_approval ...` means it was
always going to wait. `skep review <task_id>` shows the `landing:` decision,
the `re-verify:` result and, if it landed, the `approval:` line with the
actor.

## Turning it off

```sh
skep project setup ~/code/notes --phase build
skep project setup ~/code/notes --no-unlocked
```

Branches that already landed stay where they are. Delete `skep/maintain` or a
`skep/<task_id>` branch with git when you no longer want it.

## What goes wrong, and how to tell

| What you see | Cause | Fix |
|---|---|---|
| `--auto-approve has no effect: ...` on stderr, and nothing landed | the flag is retired | switch the project on (maintain phase, `--unlocked` or `auto_apply_verified_patch`) and pin a real check |
| a confirmed run in a maintain project did not land | no pinned verify command | pin one; `skep doctor` lists such projects under Advisories |
| `unlocked=true needs an approved policy baseline` | fresh home | `skep policy approve --template builder` |
| the second identical run shows `re-verify: unavailable … claimed 1 changed file(s) but deposited no patch artifact` | it started from `skep/maintain`, where the change already exists, so there was nothing to patch | nothing to fix; the work is already on the branch |
| the project's engine went back to the built-in worker after a phase change | `skep project set-phase` dropped `coding_engine` | `skep project setup <repo> --engine <engine>` |

## Limits

- A rule approves what your verify command accepts. If the suite is thin, thin
  patches land.
- Auto-landing is a local commit on a local branch. Nothing is pushed.
- The approval record carries the rule's name, not a person's. If you need a
  human on every landing, leave this off.
