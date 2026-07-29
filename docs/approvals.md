# Approvals and Learned Templates

Skep starts from narrow permissions and asks when a worker needs more. The goal is
to make the first run explicit, then make similar later runs quiet without making
the default policy broad.

## Inline Approval Prompts

When a worker hits a permission gate, `skep run` shows the approval prompt in the
same terminal:

```text
approval needed: shell.run
  reason:       shell.run requires approval for command: python -m pytest
  [a] approve once  [b] approve + remember  [d] deny  [s] skip
> 
```

- `a` resumes this run only.
- `b` resumes this run and records the permission for future template learning.
- `d` denies the request.
- `s` leaves the approval pending so you can inspect it later with `skep review`.

If the resumed run hits another gate, `skep run` prompts again in the same
terminal instead of forcing a handoff to `skep review`.

`approve + remember` does not remove the audit trail. Skep records the approval in
the durable ledger with the repo, task instructions, action, resource, actor, and
final resumed outcome.

The serve API exposes the same ledger for tooling and UI surfaces:

```text
GET /api/ledger?repo=/path/to/repo
```

## Run 1: Remember What Worked

If a remembered approval resumes successfully, Skep saves a learned template for
that repo and task pattern:

```text
resumed: original-task -> resumed-task
  state:        completed
  verification: passed
  saved template: add-login-page
```

The template captures the permissions that actually worked: network hosts, shell
allowlist entries, env allowlist entries, and git mutation if it was approved.

Use the registry commands to inspect or remove saved templates:

```sh
skep template list
skep template show add-login-page
skep template rename add-login-page auth-pages
skep template remove add-login-page
skep template delete add-login-page
```

## Run 2: Auto-Match Similar Work

On later runs, Skep tries to match a saved no-parameter template for the same repo
using simple instruction keyword overlap. If exactly one template matches and the
run has no explicit permission or budget overrides, Skep pre-grants that
template's permissions:

```text
matched template: add-login-page
task 019...
  state:        completed
```

If multiple templates match in an interactive terminal, Skep shows a one-key
picker with a `no template` option. Non-interactive runs and zero-match runs
fall back to the normal path instead of guessing.

To force the normal run path even when a template would match:

```sh
skep run /path/to/repo "add a signup page" --execution-mode workspace --no-template
```

To force a deny-all start that learns only through approvals:

```sh
skep run /path/to/repo "add a signup page" --execution-mode workspace --minimal
```

## Run 3: Remember Drift

If a matched template still needs a new permission, choose `approve + remember`.
After the resumed run completes, Skep updates the matched template instead of
creating a duplicate:

```text
updated template: add-login-page
```

This lets templates grow only when real work proves they need more permission.

## Manual Suggestion

You can preview the learned template Skep would create from remembered approvals:

```sh
skep template suggest web-feature /path/to/repo "add a signup page"
```

Add `--save` to write it:

```sh
skep template suggest web-feature /path/to/repo "add a signup page" --save
```

The serve API exposes the same preview/confirm flow:

```text
GET /api/suggestions?name=web-feature&repo=/path/to/repo&instructions=add%20signup
POST /api/suggestions/web-feature/confirm
```

`confirm` accepts `repo` and `instructions` plus optional `caste` in the JSON
body. It returns `201` with the saved template, `404` when no prior approval
profile matches the request, and `409` when the template name already exists.

## Current Boundaries

- Auto-match is intentionally conservative: same repo, no required template
  params, enough keyword overlap, and exactly one match.
- Explicit run overrides such as `--network`, `--env-allow`, or budget flags skip
  auto-match for that run.
- `--no-template` skips auto-match even if a template would otherwise match.
- `--minimal` skips auto-match and starts with empty network, env, shell, and git
  grants.
- Template narrowing, where Skep suggests removing unused permissions, is a later
  refinement.
