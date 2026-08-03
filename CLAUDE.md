# skep — assistant context

Local-first autonomous coding supervisor. A Queen (chat daemon) dispatches
disposable, contract-governed workers into isolated git worktrees; every side
effect goes through the policy/capability engine; completed work lands as a
**patch via a human approval** — landing IS the commit.

## Non-negotiable safety posture

Patch-as-approval, supervisor-side re-verification (G10), the Queen/worker
split, the trust engine, no shadow permission systems. Workers can NEVER push,
pull, fetch, or switch branches (guards: v19-F3/F5) — no allowlist, grant, or
verify label overrides those denies, and the same list binds the Queen
(v83-F9). `git add`/`git commit` are denied through `shell.run` (v22-F2) but
grantable via the explicit `git.stage`/`git.commit` capability path — safe
because the worktree is disposable and the patch diffs against the startup
baseline, not because it is absolute (see I4). G10 re-verifies with the
project's pinned `verify_command`, not the worker's own nomination (v88-F4) —
re-running the worker's choice of command is still trusting it. Since v91-F1
project setup infers that pin from the repo's own entry point, so it is the
default; a repo with no detectable entry point is pinned to nothing on
purpose and keeps the worker-nominated fallback.

## Gates (run after every fix commit, all green before moving on)

```sh
TMPDIR=$HOME/.cache/skep-test-tmp uv run --with pytest-xdist pytest -q -n auto
uv run ruff check .                               # TMPDIR outside /tmp is
uv run ruff format --check src/skep tests scripts # MANDATORY on Linux: bwrap
uv run mypy                                       # tmpfs-masks /tmp
TMPDIR=$HOME/.cache/skep-test-tmp uv run python scripts/scorecard.py  # Overall: PASS
```

(`ruff format --check` joined the list at LAUNCH-1: CI always ran it, the
local ritual never did, and 174 files drifted before the public repo's first
CI run caught it. Parallel pytest joined with the v106 port: the suite is
xdist-safe, ~1min vs ~10min serial, and CI runs the same `-n auto` line —
xdist is injected per-run by `--with`, never a project dependency. The G10
re-verify default pin stays plain `uv run pytest`; that pin is a project
policy, not this ritual.)

The suite is fully green — NO pre-existing failures are allowed. (History: the
stale-links failure was fixed in v27-F1; the Linux per-domain egress skip —
v14-7 — was closed in v28, so `test_default_coding_worker_uses_saved_
assistant_llm_config` now passes on Linux. Any failure is yours.)

## How this project evolves

Every new plan and ADR starts from `docs/invariants.md` — the traits that
must never be traded away (Part I + review checklist) and the refinement
backlog (Part II). Cite the invariant numbers a change touches.

**A new mutating verb needs an operator face** (ADR 0050, v104): reachable
from the chat tool surface AND from the CLI or REST. Four rounds fixed this
one key at a time (v94-F5, v100-F9, v101-F13, v104) before it became a rule —
I5 says one authorization boundary, not that the operator gets the narrow half
of it. `tests/supervisor/test_surface_parity.py` enforces it; genuine
exceptions go in its `CHAT_ONLY` map with a reason.

Field test → reconstruct findings from the store/audit trail → write an
executor-style plan in `plans/vNN/README.md` (observed failure, root cause
with file:line anchors, change, tests, acceptance) → implement fix-by-fix,
one commit per fix (`vNN-F<N>: title`) → push to main. `plans/` is
gitignored on the public tree: plans stay local, only the fix commits push
(the v107 plan predates this rule and remains in history). History:

- v1–v17: the roadmap (v12–v17 implemented in one unattended run; the
  authoritative record is `plans/EXECUTION_LEDGER.md`).
- v19–v24: field-test friction fixes. Read `plans/v19/README.md` for the
  house-rules briefing every later plan inherits, and skim v20→v24 for the
  landing/policy arc. v18 (public release) is intentionally not done.
- Resolved (v30, was the v24 deferral): maintain-phase auto-apply accumulates
  on ONE integration branch `skep/maintain` (project policy `auto_apply_branch`,
  constrained to the `skep/` namespace); other phases keep per-task
  `skep/<task_id>`. `main` never advances automatically. The landing branch is
  persisted on the approval (`approvals.landing_branch`) so `applied_branch`
  is accurate for every landing path.

## Operational notes

- Operator state lives in `~/.skep` (sqlite store, serve token, registered
  repos incl. the `skep-testing` test repo). NEVER mutate it in tests — use a
  temp home. Server: `uv run skep serve` (port 8765, log `~/.skep/serve.log`).
- `make` is not installed on the dev machine; call scripts via `uv run`.
- Container builds use podman (docker CLI points at an absent Docker Desktop).
- The chat Queen runs a small model (glm-5.2 via ollama.com); it skims tool
  descriptions — treat tool descriptions as load-bearing code and keep them
  truthful (a stale preset description once caused repeated "hallucinations").
- The UI is a NO-BUILD static app (`src/skep/supervisor/serve/static/`) — no
  bundler, no npm; plain ES modules + CSS tokens; fonts vendored.
- Skills (v85): `skep skill shelf add <dir>` registers external Agent Skills
  shelves (`~/.claude/skills` convention) — instruction packs load zero-grant;
  script-shipping packs draft onto the v17 ladder (`skill_packs.py`, ADR 0045)
  and activate only via `skep skill promote` / the `promote_skill_pack` card.
- Coding engines (v90, ADR 0047; hardened v94): the `coding_engine`
  project-policy key picks which agent runs a coding task — `builtin` (skep's
  own worker, defers to `SKEP_WORKER_CMD`/`--worker-cmd`) or a CLI adapter
  (`claude_code`, `codex`, `aider`); set it with `skep project setup --engine`
  (v94-F5), from chat via `setup_project engine=`, or per-dispatch via
  `dispatch_run engine=` — an explicit per-dispatch choice always cards
  (v95-F3/F4). An external agent's own commands do NOT pass the capability layer —
  the sandbox confines it — so it requires a project-pinned `verify_command`
  (its built-in verify is `git diff --check`), is FORCED into sandbox execution
  (v94-F4), and declares the env it needs in the engine registry (v94-F3:
  claude_code needs USER/LOGNAME for keychain auth). `skep doctor` probes each
  binary. Re-verify primes uv deps from the baseline before applying the patch
  so the default `uv run pytest` pin confirms offline (v94-F7).
- The command deck (v25): chat messages starting with `/` are parsed client-side
  (`COMMANDS` in app.js) and mapped onto existing verbs — the model never sees
  them. Mutations become `chat_actions` rows with `source='operator'`, resolved
  on `/api/chats/{id}/commands/{action_id}/confirm|deny` under actor
  `operator-command`. `/workon <path>` is the local-dir on-ramp (confirmed git
  baseline, then project setup). Keep `COMMANDS`, the executor, and `/help` in
  lockstep — a test pins the table against the parser branches.

## Where to look first

`plans/` (all plans + `EXECUTION_LEDGER.md`), `docs/version-history.md`,
`src/skep/supervisor/policy_resolver.py` (run policy), `serve/actions.py`
(shared verbs), `serve/tools.py` (chat tool surface), `workers/capabilities.py`
(worker-side gates), `apply.py` (landing).
