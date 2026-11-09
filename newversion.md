# unlocked-F7 — `SKEP_UNLOCKED`: the global capability probe + gate instrument

A reversible, process-wide toggle that turns skep into a capability probe: the
friction gates *contained by the sandbox* auto-resolve, every gate records what
it WOULD have decided, and a report tells you which gates protect something real
versus which only add friction. Built on top of the per-project `unlocked`
(ADR 0052), not instead of it.

> **⚠ FOR LOCAL CAPABILITY TESTING ONLY — NEVER PRODUCTION.** Unlocked mode
> bypasses governance. It exists to measure the policy layer, not to run real
> work. Default is OFF; leaving it off is byte-for-byte governed skep.

## Turn it on

```sh
export SKEP_UNLOCKED=1        # sandbox tier
skep serve
```

Config equivalent (survives restarts, no shell export): add one line to
`~/.skep/serve.env` — `skep serve` loads it into the process env at boot:

```sh
echo 'SKEP_UNLOCKED=1' >> ~/.skep/serve.env
```

Accepted truthy values: `1`, `true`, `on`, `yes`, `sandbox`, `host`, `all`.
Anything else (including unset) is OFF, fail-closed. `host`/`all` are accepted as
the sandbox tier for now — see *Limitations*.

You cannot miss it: `skep serve` prints a loud banner at boot (and writes it to
`~/.skep/serve.log`), `skep doctor` names it, and the chat policy strip still
shows the `UNLOCKED` pill (ADR 0052 F6).

```
======================================================================
  ⚠  SKEP UNLOCKED — GOVERNANCE BYPASSED  (tier=sandbox)
  Capability probe for LOCAL testing ONLY. NEVER production.
  ...
======================================================================
```

## What the sandbox tier does

**Auto-resolves (the sandbox contains these):**
- Dispatch cards — `dispatch_run` / `run_code` / `quick_edit` / batch members run
  in-turn (reuses the ADR 0052 `project_policy_dispatch_decision` path).
- Worker escalations — shell / capability / network-host asks answer themselves
  (the same `resume_past_gate` a click takes, actor `auto:unlocked`, granted once).
- Verified-patch landing — zero clicks, still G10-confirmed with the *pinned*
  verify_command, onto `skep/*` only.
- Run egress — opens to all hosts (`["*"]`) so the worker can reach anything; the
  G2 env allowlist still strips credentials, so it cannot authenticate.

**Never flipped — logged, never bypassed (this is the point):**
- The never-grantable deny floor: `git push/pull/fetch`, branch switch, history
  rewrite, `git add`/`commit` via shell, sudo, catastrophic commands,
  workspace-escape. A deny in the report means *this gate protects something real*.
- The safety core: G10 re-verification, landing-branch validation (skep/* only,
  never the default/checked-out branch), the sandbox wall (unlocked runs are
  sandbox-forced; a required sandbox that can't be built still refuses).
- Outbound content (open_pr, merge_pr, email/messenger sends), Queen host shell
  (`run_shell` runs on the REAL host, not the sandbox), and policy/trust
  mutations still card — the sandbox argument does not cover these. They are
  logged as `STILL ASKED` (host-tier candidates).

## Read the log back

Every bypassed or logged gate is one JSONL line under
`~/.skep/supervisor/audit/unlocked/<session>.jsonl`:

```json
{"ts":"2026-08-16T14:03:11Z","session":"pid8123","run_id":"c1a2",
 "gate":"dispatch_run","surface":"queen_card","would":"allow",
 "reason":"dispatch.auto_allowed.unlocked","tier":"sandbox",
 "action":{"verb":"dispatch_run","args":{"repo":"my-portfolio"}},
 "disposition":"bypassed"}
```

`skep unlocked-report` groups it into the three buckets that answer the question:

```
UNLOCKED REPORT — 143 gate hits across 6 run(s), 1 session(s)

WOULD HAVE DENIED (protecting something real — do NOT relax):
  worker.escalation.shell.run     4x   ...git push origin...

AUTO-ALLOWED BY UNLOCKED (a wall it skipped — friction, ranked):
  dispatch_run                   40x   ...
  worker.escalation.shell.run    22x   pytest · uv run
  network.egress                 18x   ...

STILL ASKED (carded even under the sandbox tier — host-tier candidates):
  open_pr                        12x   ...

NEVER DENIED (only ever ask/allow — safe to relax in governed mode):
  create_branch, read_file, ...
```

`skep unlocked-report --session pid8123` scopes to one daemon run.

## Turn it off

Unset the env var (and remove the `serve.env` line), restart serve. Every code
path added here is `if unlocked_active(): ...` around the existing return, so OFF
is a dead branch — governed skep is byte-for-byte main. A test pins this
(`tests/supervisor/test_unlocked_global.py::test_off_does_not_inject_unlocked`).

## Limitations (v1, deliberate)

- **Chat + serve-dispatched runs are fully instrumented.** CLI (`skep run`) and
  scheduler dispatches auto-resolve the same way (they read the same resolved
  policy) but do not add a chat-card JSONL line; their auto-verdicts are still in
  the approvals ledger and born-resolved rows (ADR 0052 instrumentation).
- **Worker-side deny-floor hits** (git push, catastrophic) that fail a run without
  reaching the supervisor as an approval are not written to this JSONL in v1 —
  they live in the per-run blocked-command audit events. The report's WOULD-HAVE-
  DENIED bucket is populated from Queen-side denies and answered escalations.
- **The host tier is not built.** `SKEP_UNLOCKED=host` currently behaves as the
  sandbox tier. Flipping outbound content, Queen host shell, and policy mutations
  is a separate, louder opt-in left for a follow-up.

## Files

- `src/skep/supervisor/unlocked.py` — the toggle, logger, banner, report, CLI.
- `policy_resolver.py` — inject `unlocked` globally; open run egress.
- `serve/chat.py`, `serve/actions.py` — instrument the card gate and worker
  escalations.
- `serve/serve_cmds.py` — boot banner. `cli_cmds.py` — `unlocked-report`.
  `status.py` — doctor advisory. `tests/supervisor/test_unlocked_global.py`.
