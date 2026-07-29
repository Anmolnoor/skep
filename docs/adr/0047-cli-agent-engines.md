# 0047 — CLI-agent engines and where their authority boundary is (v90)

## Status

Accepted (v90-F1).

## Question

skep has shipped adapters for Claude Code, Codex and Aider since v33,
all complete, all unreachable: nothing mapped a name to them, so the
only way to run one was `SKEP_WORKER_CMD`, which replaces the default
coding worker process-wide for every run. The operator asked for a way
to actually choose one.

Registering them is not a config change. An external agent's own
commands do **not** pass skep's capability layer, so admitting one
changes which wall is doing the confining — and **I5** ("one
authorization boundary; no shadow permission systems") is exactly the
invariant that should catch a change of that shape. Hence a decision
record rather than a table entry.

## What is actually true

`cli_adapter.py` runs the external binary with a plain
`subprocess.run`. The worker-side git guards (`runtime_plugins.py` —
v19-F3 remote git, v19-F5 branch ops, v22-F2 add/commit) live inside
`shell.run` capability handling, which only skep's first-party workers
route through. A CLI agent never touches that path.

What still binds it is the **sandbox**, inherited because the whole
worker process tree runs under bubblewrap/Seatbelt:

| Wall | Built-in worker | CLI engine |
|---|---|---|
| Workspace-only writes | capability layer + sandbox | **sandbox** |
| Network pinned to the allowlist | capability layer + sandbox | **sandbox** |
| No remote git | capability deny (absolute) | **network pin** |
| No branch switch | capability deny (absolute) | not enforced |
| `git add`/`commit` | denied via `shell.run` | not enforced |
| Landing requires human approval | **I1** | **I1** |

## Decision

**1. Engines are a registry, chosen per project.** `coding_engine`
joins `PROJECT_POLICY_KEYS`, becomes a typed field on
`ResolvedRunPolicy`, and is validated at the resolver — an unknown name
fails closed naming the valid choices, never a silent fallback to the
coding worker (the v42 lesson, where an unregistered caste did exactly
that and the run was rejected downstream with no useful reason).

**2. `builtin` defers to `config.command_for`.** The built-in engine
carries an empty argv on purpose. `SKEP_WORKER_CMD`, `--worker-cmd`,
and the test fake worker all override `config.worker_command`, and an
engine that hardcoded skep's worker would quietly take that away. Only
an *external* engine replaces the argv.

**3. The engine's API host is merged into the allowlist.** v19-F2's
rule applied to the agent's own provider: an agent that cannot reach
its API cannot work at all, and without the merge the failure is a
confusing timeout rather than a stated denial (**I12**).

**4. A CLI engine requires a project-pinned `verify_command`.** Its
built-in verification is `git diff --check` — whitespace and conflict
markers. Re-verifying that under G10 re-runs the whitespace check, so
`confirmed` would mean nothing. This is the one place v88-F4's opt-in
is mandatory, because the vacuous verify is the adapter's design rather
than a worker's bad choice. Resolution refuses with that reason stated.

**5. `skep doctor` probes every engine's binary.** Reported absent
before dispatch, naming what was probed — the v87-F6 lesson, where a
binary that was not on the host burned three runs before anything said
so.

## Why this does not violate I5

I5 forbids a feature carrying its own *private allow-logic*. A CLI
engine carries none: it makes no policy decisions, and every decision
about it — whether it may run, what it may reach, whether its result
may land — is made by the same policy resolver, sandbox profile, and
approval gate as any other run. What changes is which of skep's
existing walls enforces a given rule for that lane, and that table is
written down above rather than left to be discovered.

The honest cost: for a CLI-engine run, "workers can never commit" holds
because the worktree is disposable and the patch is a working-tree diff
(the v88-F5 reasoning), not because a deny fires. "Workers can never
reach a remote" holds because the network pin excludes one, not because
`git push` is refused by name. **I1 is untouched** — the patch still
lands only through a human approval — and I1 is what makes both of
those consequence-free.

## Consequences

- An operator can run Claude Code on a project by setting two policy
  keys, and `skep doctor` says up front whether the binary is there.
- A project that chooses an external engine must state what
  verification means. That is a real friction and it is the point.
- The guard-list invariant (I4) keeps its absolute reading only for
  first-party workers; `docs/invariants.md` I4 already distinguishes
  what is absolute from what is consequence-free (v88-F5), and this
  record is what that distinction now points at for CLI lanes.
- If a future adapter runs an agent that can reach the network outside
  the pin (its own proxy, a daemon), this record does **not** cover it
  — that would need the capability layer, not a new registry entry.
