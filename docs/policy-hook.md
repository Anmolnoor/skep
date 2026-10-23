# The policy enforcement hook (P3, ADR 0053)

skep's `decide()` reachable from outside the daemon, and recorded. An agent
running on the workstation — Claude Code first — asks skep before every
gated tool call; the answer is the same rule a dispatched worker would hit,
and every answer is a line in the audit trail:

```
<home>/supervisor/audit/policy/decisions.jsonl
```

Three faces, one function (`serve/actions.py::policy_decide`, I5):

| Face | Call |
|---|---|
| REST | `POST /api/policy/decide {"scope","action","value","repo"?,"actor"?}` |
| CLI | `skep policy decide SCOPE ACTION VALUE [--repo REPO] [--actor NAME]` |
| Hook | `skep policy hook` — Claude Code PreToolUse payload on stdin, hook JSON on stdout |

## Which policy answers

- **The floor first (I4).** For `shell`, the Queen's own refusal set — the
  worker git guards (push/pull/fetch, add/commit, branch switches, history
  rewrites), privilege escalation, the catastrophic commands — is checked
  over every segment of the command line before any document is read. A hit
  is `deny` by the registry's floor key (`floor.worker-git-hard-denies` or
  `floor.never-grantable-prefixes`), for the run and the Queen alike, and
  nothing in any layer can lift it.
- With a `repo` (the hook passes Claude Code's `cwd`), the **run policy**
  decides for `shell` and `filesystem`: the same `resolve_run_policy` a
  dispatch there runs, then `decide()` on the compiled document (the why
  query of ADR 0052). The record says `"source": "run"` and names the
  project.
- `network.search` and `network.fetch` are Queen-side lanes by definition,
  so the **operator document** decides them, repo or not.
- A path no project binds still answers from the run policy — the global
  layer alone — and the record notes it is unbound (I8).
- Without a repo, when the path cannot be resolved at all, or when the run
  policy will not resolve (for example `execution_mode: ask`), the operator
  document decides and the record carries a `note` saying why.

## Installing the Claude Code hook

In `~/.claude/settings.json` (or the project's `.claude/settings.json`):

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash|Edit|Write|MultiEdit|NotebookEdit|WebFetch|WebSearch",
        "hooks": [
          { "type": "command", "command": "uv run --project /path/to/skep skep policy hook" }
        ]
      }
    ]
  }
}
```

The hook reads the operator's own home (`~/.skep`), the same store the CLI
reads; the daemon does not need to be running.

## What the hook maps

| Tool | scope.action value |
|---|---|
| `Bash` | `shell.run <command>` |
| `Edit`, `Write`, `MultiEdit`, `NotebookEdit` | `filesystem.write` — `workspace` when the path is inside `cwd`, the literal path otherwise |
| `WebFetch` | `network.fetch <host>` |
| `WebSearch` | `network.search <query>` |
| `Read`, `Glob`, `Grep`, anything else | no opinion — the hook prints nothing and Claude Code's own permissions decide |

Reads are a contract permission (`read: ["workspace"]`), not a document
rule; a hook that denied every read would be inventing policy.

## What the verdict does

| skep verdict | hook `permissionDecision` |
|---|---|
| `allow` by a rule | `allow` |
| `deny` by the floor (`git push`, `sudo …`, `rm -rf /`) | `deny` — Claude Code refuses the call and names the floor key |
| `deny` by an explicit deny rule | `deny` — Claude Code refuses the call and shows the rule |
| `require_approval` | `ask` — Claude Code asks the human |
| unmatched (`default-deny`) | `ask` — the shape skep gives its own workers: an unlisted command cards, an explicit deny refuses |
| skep could not decide (bad home, broken store) | `ask`, with the error as the reason — towards the human, never a silent allow |

The reason string names the rule (`skep: allow by trusted_local_dev/shell:uv
run pytest (run policy)`), so `skep policy why` explains any surprise.
