# plan — `SKEP_UNLOCKED`: a capability probe that instruments the policy layer

Status: **awaiting your review. No code written yet.**

## TL;DR

The task asks for a global `SKEP_UNLOCKED=1` toggle that resolves every gate to
ALLOW, logs what each gate *would* have decided, prints a loud banner, and reads
back as a report. Four subagents mapped every gate (worker + supervisor). Two
findings reshape the build:

1. **The branch already has a per-project `unlocked` (ADR 0052)** that bypasses
   three gate families (dispatch cards, worker escalations, patch landing) and
   *records every auto verdict to the ledger*. It is careful and per-project; it
   has **no global toggle, no gate-instrumentation log, no banner, no report**.
   The new work is a superset toggle + the instrument, reusing its machinery.

2. **The "what it would have decided" data mostly already exists** — born-resolved
   receipts, the approval ledger, per-run blocked-command audit events, and RSoP
   provenance. All four agents independently said: don't build a new record
   system (invariant I5 forbids one). The instrument is a thin log at ~4 choke
   points plus a reader — not a new subsystem.

## The one decision I need from you

The task says "every gate resolves to ALLOW." Taken literally that includes gates
that (a) run on the **real host, not the sandbox** (Queen shell, `run_shell`),
(b) **leave the machine** (open_pr, merge_pr, email/messenger sends), and (c) the
**never-grantable deny floor** (git push/pull/fetch, sudo, catastrophic `rm`,
workspace-escape). Forcing (c) to ALLOW isn't "non-destructive" — it's the
destructive event itself, and it can't be done without gutting the floor code the
whole safety model rests on. (a)/(b) are real capabilities but they act *outside*
the sandbox that makes unlocked defensible.

So I propose **tiers**, and you pick how far the default reaches:

| Tier | Env | Bypasses to ALLOW | Instruments (logs would-be verdict) |
|---|---|---|---|
| **off** (default) | unset | nothing | nothing — byte-for-byte governed |
| **sandbox** (recommended default when set) | `SKEP_UNLOCKED=1` | the friction `ask` arm of everything **contained by the sandbox**: worker escalations, dispatch cards, in-run capability asks, run egress, MCP asks, the landing click (via the existing verified-patch lane) | **every** gate, including the deny floor |
| **host** (explicit opt-in) | `SKEP_UNLOCKED=host` | also the host-reaching asks: Queen `run_shell`, outbound content (PRs, email), `read_url`, policy/trust mutations, operator `/command` cards | every gate |
| **never, any tier** | — | the never-grantable deny floor + the safety-critical landing/G10/sandbox-wall checks | logged, never flipped |

**Why not literal "everything → ALLOW":** a gate that *denies* is, by definition,
protecting something real — that's the answer to your primary question, and you
get it by **logging** the deny, not by flipping it. Flipping the floor deletes the
signal *and* the safety. The sandbox tier still exercises the full worker
capability surface (that's the raw-capability test); the host tier adds the
outward-facing reach behind a louder switch. If you want a single flag that
reaches host+outbound too, say so and I collapse `host` into `=1`.

Everything below is written for the tiered design; collapsing tiers is a one-line
change to `unlocked_tier()`.

---

## Design

### 1. The toggle (single, reversible, default off)

New module `src/skep/supervisor/unlocked.py` — the whole feature lives here plus a
handful of one-line call sites:

```python
def unlocked_tier(store=None) -> str:          # "off" | "sandbox" | "host"
    env = os.environ.get("SKEP_UNLOCKED", "").strip().lower()
    if env in {"1","true","on","sandbox"}: return "sandbox"
    if env in {"host","all"}:              return "host"
    if env in {"0","false","off",""}:
        # config equivalent: a store setting, env always wins
        if store is not None and store.get_setting("unlocked_global") in {"sandbox","host"}:
            return store.get_setting("unlocked_global")
        return "off"
    return "off"                                # unknown value fails closed

def unlocked_active(store=None) -> bool: return unlocked_tier(store) != "off"
```

- **Env is primary**; the config equivalent is a store setting `unlocked_global`
  (set via a `skep unlocked on|off` CLI or left unset). Env overrides config.
- **Default off ⇒ zero behavioral change.** Every call site is
  `if unlocked_active(): <log + maybe allow>` wrapped around the *existing*
  return. Off → dead branch → governed skep byte-for-byte. This is the
  reversibility guarantee, and a test pins it (governed-mode snapshot with the
  env unset == main).

### 2. How the toggle reaches the gates

Two mechanisms, deliberately separate:

**(a) Reuse the per-project `unlocked` key for the sandbox-contained families.**
Inject at one point — `run_policy_for_repo` (`policy_resolver.py:175`): if
`unlocked_active()`, set `effective["unlocked"] = True` before provenance is
stamped (provenance layer label `"unlocked-global"`). All **9 existing**
`policy.get("unlocked") is True` read sites (dispatch `autonomy.py:123`, landing
`policy_resolver.py:262`, sandbox-force `:625`, escalation auto-resume
`actions.py:2830`, doctor, UI, …) light up for free. The dispatch-time
fail-closed checks still fire: no sandbox → run refused (`dispatch.py:412`),
unpinned verify → landing still blocks (`policy.py:108`). Safe degradation is
preserved; the global flag can't *weaken* the wall, only skip the asks the wall
already contains.

**(b) The card layer + egress need their own intercept** — ADR 0052 deliberately
leaves these carding, so the per-project key doesn't cover them. Two edits:

- `mutation_execution_decision` (`serve/tools.py:4793`) — the single oracle for
  all 79 chat mutating verbs (66 card by default via the tail `return None` at
  `:4925`). Wrap it: compute the real decision, **log it**, then return `allow`
  unless the real verdict is `deny` — and gate *which* verbs flip on the tier
  (sandbox tier: only verbs whose args resolve to a repo and stay in-sandbox;
  host tier: also outbound/policy/Queen-shell). Deny-space stays unreachable:
  `chat.py:1525` and the per-verb last guards (`tools.py:4059`, `:3427`, `:3553`)
  run *after* this and are untouched.
- `resolve_run_policy` egress branch (`policy_resolver.py:627`) — if
  `unlocked_active()`, `chosen_network = ["*"]` and log the host list it would
  have resolved. Flows automatically into `NetworkPolicy → ALLOW_ALL_NETWORK`;
  `netproxy`/`sandbox` enforcement code is untouched (it just enforces `"*"`).

Worker-side (`_shell_decision` etc., `capabilities.py:665`) needs **no change**:
those `require_approval` asks already round-trip through `auto_resume_unlocked_gate`
under (a). Touching the worker is a latency optimization, explicitly out of scope
for v1 (keeps the diff off the worker contract).

### 3. Instrumentation (the point)

One append-only JSONL per serve session:
`~/.skep/audit/unlocked/<session>.jsonl` (`config.audit_dir` = `~/.skep/audit`).
One helper `record_gate(...)` in `unlocked.py`, called at each intercept. Schema:

```json
{"ts":"2026-08-12T14:03:11Z","session":"srv-8f2a","run_id":"<task_id|chat_id|queen>",
 "gate":"mutation_execution_decision","surface":"queen_card",
 "would":"ask","reason":"card.default","tier":"host",
 "action":{"verb":"open_pr","scope":"repo:my-portfolio","target":"branch skep/abc"},
 "disposition":"bypassed"}      // bypassed | logged_only
```

- `would` ∈ {allow, ask, deny} is the gate's **real** verdict (computed, then
  overridden). `disposition` says whether we honored or bypassed it.
- Written at the (a)/(b) supervisor choke points. **Deny-floor events** (worker
  git push, catastrophic, etc.) are `logged_only` and, where they never reach the
  supervisor, the report also reads them from the existing per-run blocked-command
  audit events (`policy_block_views`, `actions.py:1846`) — no new worker logging
  in v1. This is called out in `newversion.md` as the one place data is read from
  the store rather than the JSONL.
- No new sqlite table (I5). The JSONL is self-contained so the report needs only
  one file, and it survives independent of the store schema.

### 4. Banner (unmissable)

`unlocked_banner(tier)` in `unlocked.py`, printed to stderr wherever an unlocked
run can begin:

- `skep serve` boot (`cli.py:294`) — red multi-line box: **"⚠ SKEP UNLOCKED
  (tier=host) — governance bypassed, capability probe only, NEVER production. Log:
  ~/.skep/audit/unlocked/<session>.jsonl"**.
- CLI dispatch commands and `skep chat` startup.
- Chat surface: the existing `UNLOCKED ·` policy pill (`app.js:2421`) gets a
  `GLOBAL` prefix + a first-message system note in every chat while active.

### 5. Report

`skep unlocked-report [--session <id>] [--since <ts>]`, registered in
`register_supervisor_commands` (`cli_cmds.py`) next to `doctor`. Pure-python
aggregation over the JSONL (+ blocked-command audit for deny-floor hits), grouped
by `gate`:

```
UNLOCKED REPORT — session srv-8f2a — 143 gate hits across 6 runs

WOULD HAVE DENIED (protecting something real — do NOT relax):
  git.floor.remote            4x   git push origin (worker, run abc12)  ...
  catastrophic.command        1x   rm -rf ~ (worker, run def34)

MOST-HIT ASKS (bypassed; friction candidates, ranked):
  mutation_execution_decision 61x  dispatch_run 40 · land_run 12 · create_branch 9
  worker.shell.require_approval 22x  pytest 14 · uv run 8
  network.egress               18x  api.github.com 11 · pypi.org 7

ASK-ONLY, NEVER DENIED (safe to relax in governed mode):
  read_url                     7x   (host tier only)
  create_branch                9x
```

Three buckets answer your primary question directly: **would-deny** = keep,
**ask-only** = relaxation candidates, **most-hit** = where the friction is.

---

## The gate atlas + disposition

Every gate the four agents found, with what unlocked does to it. Legend:
**BYPASS-s** = flip `ask`→allow at sandbox tier · **BYPASS-h** = flip at host tier
only · **LOG** = record would-be verdict, never flip · **LEAVE** = not an ask /
enforcement plumbing / already handled.

### Sandbox-contained friction — BYPASS-s (+ LOG)
| Gate | file:line | Real verdict | Via |
|---|---|---|---|
| Project dispatch decision | `autonomy.py:105` | ask | already `unlocked` (a) |
| dispatch_run / run_code / quick_edit / batch | `serve/tools.py:4809`+, `actions.py:3456` | ask | (a) + (b) card oracle |
| Worker escalation → pending_approval | `ingest.py:244` → `actions.py:2830` | ask | already `auto_resume_unlocked_gate` (a) |
| Worker shell `require_approval` arm | `runtime_plugins.py:650` / `capabilities.py:665` | ask | round-trips via (a); worker edit deferred |
| Worker plugin / git.stage / network asks | `capabilities.py:691/751/780` | ask | round-trips via (a) |
| Run egress allowlist | `policy_resolver.py:627` | ask/deny-host | (b) → `["*"]` |
| MCP scope decision (`require_approval` arm) | `mcp_client.py:472/522` | ask | (b) card oracle |
| Landing approval click | `dispatch.py:798` | ask | already `auto_apply_verified_patch` (a) |

### Host-reaching friction — BYPASS-h only (+ LOG)
| Gate | file:line | Real verdict | Note |
|---|---|---|---|
| Queen `run_shell` / `start_process` | `serve/tools.py:2488` | ask | **real host, not sandbox** — ADR 0052 row 6 excludes it |
| Outbound content (open_pr, merge_pr, email/messenger) | `tools.py:4925`, `shell_prefixes.py:123` | ask (card) | **leaves the machine** — ADR 0044 |
| `read_url` fetch grant | `tools.py:2429` | ask | Queen egress |
| Queen filesystem write | `serve/fileio.py:53` | ask | host FS |
| Policy/trust mutations (set_policy, set_operator_policy, promote_skill_pack) | `cards.py:_POLICY_TOOLS` | ask | model widening its own cage (I6) — flip only if you explicitly want it |
| Operator `/command` cards | `chat.py:2355` | ask | I7 says a typed command is already the decision |

### Never flipped, any tier — LOG only
| Gate | file:line | Why it stays |
|---|---|---|
| Git floor: push/pull/fetch, checkout/switch, merge/rebase/reset --hard, add/commit-via-shell | `runtime_plugins.py:458-531` | never-grantable (v19-F3/F5, v103-F3, v22-F2) |
| Catastrophic command (whole-line + per-segment) | `runtime_plugins.py:568/596`, `shell_prefixes.py:277/320` | destroys the host |
| Queen shell / command-line hard refusals | `shell_prefixes.py:545/579` | I4 |
| Persist-time refusals (`dangerous_prefix_reason`, `hard_denied_segment_reason`), grant sweep | `shell_prefixes.py:464/499/617` | keeps denies out of storage |
| Learn-into-deny vetting | `policy_schema.py:277`, `actions.py:3607` | can't learn into denied space |
| Workspace-escape, unknown capability, safe-risk side-effect | `capabilities.py:504/488/1193` | argument-level, not policy |
| Provider-endpoint egress allowlist | `llm_plan.py:289` | I12 |
| Researcher / document / reviewer source allowlists | `researcher.py:72`, `document.py:122`, `reviewer.py:220` | caste egress floor |
| **G10 supervisor re-verification** | `reverify.py:109/272`, called `dispatch.py:739` | **the safety core** |
| Pinned-vs-worker verify choice | `reverify.py:296` | I2 |
| `rule_block_reason` (require_reverified, require_pinned_verify, risk, scope) | `policy.py:100-120` | landing safety |
| Landing-branch validation, one-shot apply guard | `apply.py:136/181` | I1 |
| Recovery never replays auto-apply | `dispatch.py:974` | crash safety |
| Deny arm of the card oracle | `chat.py:1525` | deny-space unreachable by confirm |
| Card timeout auto-DENY (300s) | `ticker.py:111` | I6 (removing the card is the fix, not weakening the timeout) |

### Not a gate / enforcement plumbing — LEAVE
Budgets & hollow-plan/trace (`coding_minimal.py`), netproxy/sandbox enforcement
(`netproxy.py`, `sandbox.py`), policy-group composition & ungroupability
(`projects.py:282`), skill-pack ladder promotion gate (`plugin_lifecycle.py`),
channel confirm allow-list (transport auth), ops decision engine
(`workers/ops.py` — supervisor-invoked, its own review), the G2 env allowlist
(`spawner.py:56` — the thing that keeps credentials out even under `["*"]` egress;
**must stay**, it's what makes the sandbox-tier egress test safe).

---

## Files touched (small, additive, reversible)

- **new** `src/skep/supervisor/unlocked.py` — `unlocked_tier/active`, `record_gate`,
  `unlocked_banner`, `build_report`. ~120 lines, the whole feature.
- `policy_resolver.py` — 2 edits: inject `unlocked` in `run_policy_for_repo`;
  `["*"]` egress in `resolve_run_policy`. ~8 lines.
- `serve/tools.py` — wrap `mutation_execution_decision` (log + tiered bypass). ~15 lines.
- `cli.py` / `cli_cmds.py` — banner at serve/CLI boot; register `unlocked-report`
  (+ optional `unlocked on|off`). ~30 lines.
- `serve/static/app.js` — `GLOBAL` prefix on the existing UNLOCKED pill. ~3 lines.
- `newversion.md`, `README` note — run + read instructions, "local capability
  testing only, never production" warning.
- tests: `tests/supervisor/test_unlocked_global.py` — off==governed snapshot;
  tier gating; JSONL shape; report buckets. Reuses the temp-home fixture; the
  existing `test_unlocked_mode.py` stays green (defaulted-off).

## Out of scope (noted, not fixed here)

Two latent bugs in the *existing* ADR 0052 code, surfaced during mapping — real
but unrelated to this toggle; flagging so they're on record:
1. `copy_project_policy` (`actions.py:863`) writes via `add_project_policy`
   directly, bypassing `unlocked_block_reason` — it can install `unlocked=true`
   with no pinned verify (fails safe: escalations auto-answer but nothing lands).
2. `auto:unlocked` ledger rows feed the v112 "remember this?" nudge
   (`actions.py:2994`) unfiltered — an unlocked project generates promotion nudges
   for grants nobody clicked.

Worker-side per-gate JSONL (in-process logging from inside the sandbox) is
deferred; v1 logs supervisor-side and reads deny-floor hits from existing audit
events.

## Reversibility & safety recap

- `SKEP_UNLOCKED` unset ⇒ `unlocked_tier()=="off"` ⇒ every intercept is a dead
  branch ⇒ governed skep is byte-for-byte main. One test asserts it.
- The never-grantable floor, G10, the sandbox wall, and the landing-branch rule
  are **never** flipped — they're logged. A required sandbox that can't be built
  still refuses (`spawner.py:175`); an unpinned project still can't auto-land.
- The banner + report make "nothing asked" always read as *the mode you turned
  on*, never as "nothing happened" — the I8 honesty ADR 0052 already insists on.
