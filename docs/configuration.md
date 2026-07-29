# Configuration

Skep keeps local state under one home directory and resolves runtime knobs at the
process edge. The default home is `~/.skep`.

## The Vocabulary: Policy, Scope, Gate, Template, Audit

Skep's user-facing language is administrative (v40, executing the v36
policy-first plan; internal names — Queen, worker, honeycomb — stay in code
and dev docs):

- **Policy** — the rules: one unified document (`skep setup --template`,
  the settings key) plus the legacy knobs it compiles into.
- **Scope** — where a rule applies: `coding`, `shell`, `filesystem`,
  `network`, `mcp`, `email` (live since v41-F3: an email-bound MCP server's
  tools decide as `email/read` or `email/send`).
- **Gate** — a `require_approval` verdict made visible: the approval queue,
  confirm cards, and `skep review`.
- **Template** — a named, inspectable policy document (`locked-down`,
  `personal-dev`, `homelab-ops`, `assistant`), diffable on switch.
- **Audit** — always on: every decision records `decided_by`, the rule that
  produced it.

## Home Directory

Choose a home with either the global flag or environment variable:

```sh
skep --home ~/.skep-dev status
SKEP_HOME=~/.skep-dev skep status
```

The top-level home stores personal setup and serve-level assets:

```text
~/.skep/
  profile.json
  memory/
  runs/
  artifacts/
  repos/
  supervisor/
    supervisor.sqlite3
    audit/
    results/
    worktrees/
    llm-secret
```

`skep setup --personal` writes `profile.json` and the basic local directories.
The supervisor database, audit records, worker results, and worktrees live under
`<home>/supervisor`.

## Personal Provider Profile

Configure the personal profile from the CLI:

```sh
skep setup --personal \
  --provider ollama \
  --model llama3.2 \
  --endpoint http://localhost:11434
```

For providers that need a credential, store only the environment variable name:

```sh
skep setup --personal \
  --provider openai-compatible \
  --model gpt-4.1-mini \
  --endpoint https://api.example.com/v1 \
  --api-key-env EXAMPLE_API_KEY
```

`profile.json` stores the provider name, model, endpoint, and credential
environment variable name. It does not store the credential value.

## Worker Command

The worker command decides which coding agent Skep supervises.

Per run:

```sh
skep run --worker-cmd "python -m skep.workers.claude_code" /path/to/repo "fix the bug" --execution-mode workspace
```

For the current shell:

```sh
export SKEP_WORKER_CMD="python -m skep.workers.claude_code"
```

For the serve UI, edit the worker command in Settings. The setting is persisted
in the supervisor database and used for later dashboard runs.

The Claude Code adapter also supports:

```sh
export SKEP_CLAUDE_CODE_CMD="/path/to/claude"
```

Allow it on runs that need the custom executable:

```sh
skep run \
  --worker-cmd "python -m skep.workers.claude_code" \
  --env-allow SKEP_CLAUDE_CODE_CMD \
  /path/to/repo \
  "fix the bug" \
  --execution-mode workspace
```

## Execution Mode

Runs can execute in either mode:

- `sandbox`: run the worker through the host sandbox backend.
- `workspace`: run without the host sandbox, still in Skep's disposable
  worktree and approval/re-verification pipeline.

Choose per run:

```sh
skep run /path/to/repo "fix the bug" --execution-mode sandbox
```

The serve Settings page also has `default_execution_mode`:

- `ask`: require each run request to choose.
- `workspace`: default dashboard/API runs to workspace mode.
- `sandbox`: default dashboard/API runs to sandbox mode.

Sandbox mode fails closed if the requested host backend cannot be applied.

## Permission Defaults

Per-run flags override defaults:

```sh
skep run /path/to/repo "update dependencies" \
  --execution-mode workspace \
  --network pypi.org \
  --env-allow EXAMPLE_API_KEY
```

The serve Settings page can persist these policy defaults:

- `default_network`: network hosts granted to runs without explicit network
  flags
- `default_env_allowlist`: environment variables passed to workers by default
- `allowed_shell_commands`: argv prefixes that can be approved by policy
- `allowed_plugin_risks`: plugin risk classes allowed by policy
- `trusted_workspace_roots`: host roots considered trusted for workspace-mode
  decisions
- `sandbox_required_for`: risk labels that force sandbox mode

Use explicit run flags when you want one run to differ from the stored policy.

### Shell allowlist and remembered commands

`git push`, `git pull`, and `git fetch` can never be allowlisted or remembered
(v19-F3). The worker denies them outright, and Skep lands changes as a patch on
a `skep/<task_id>` branch after approval — a worker never touches your remotes.
The one-click `git` preset therefore no longer includes push, and a store that
still carries a poisoned `["git","push"]` entry (from an older grant) has it
filtered from every read and swept out on daemon startup with a logged warning.

Approving a pending shell command with **Allow & remember** persists the exact
normalized command, not a broad prefix (v19-F4): a leading `git -C <path>` is
stripped so a dead absolute worktree path can never be stored, and the command
runs through the same too-broad guard as the policy editor. When the run's repo
is bound to a project (`skep project`), the remembered command lands in that
**project policy's** `allowed_shell_commands` rather than the global setting, so
remembering one repo's command does not widen every other repo's allowlist.
Remembering falls back to the global setting only when the repo is not bound.

## Budgets

Templates, schedules, and serve policy can set default budgets:

- wall-clock seconds
- max iterations
- max actions
- max provider calls

Per-run or per-template budgets should be narrow enough to bound a stuck worker,
but large enough for the expected task.

## Auto-Approval

`--auto-approve` and the serve Settings auto-approve toggle enable Skep's safe
verified-patch policy. Auto-approval still requires successful worker
verification, successful supervisor re-verification, and the configured policy
conditions.

If re-verification is missing or disagrees with the worker, auto-approval does
not apply.

## Assistant Settings

The browser dashboard can store assistant model settings for chat and for the
default in-repo coding worker when no explicit worker profile exists.

- base URL, protocol, and default model are stored in the supervisor database
- API key is stored in `<home>/supervisor/llm-secret` with `0600` permissions
- `SKEP_LLM_API_KEY` overrides the saved key when set

## Useful Environment Variables

| Variable | Purpose |
| --- | --- |
| `SKEP_HOME` | Default top-level Skep home when `--home` is not passed. |
| `SKEP_WORKER_CMD` | Default worker command for CLI and serve startup. |
| `SKEP_PROVIDER` | Default `setup --personal --provider` value. |
| `SKEP_MODEL` | Default `setup --personal --model` value. |
| `SKEP_PROVIDER_ENDPOINT` | Default provider endpoint for setup. |
| `SKEP_PROVIDER_API_KEY_ENV` | Default provider credential env-var name for setup. |
| `SKEP_LLM_API_KEY` | Dashboard assistant API key override. |
| `SKEP_CLAUDE_CODE_CMD` | Claude Code adapter executable path. |

For first-run commands, see [`quickstart.md`](quickstart.md). For sandbox
details, see [`sandboxing.md`](sandboxing.md).
