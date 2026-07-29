# ADR 0007 — Auto-approval policy rules (D3, v2 mechanism)

Date: 2026-06-11 · Status: accepted

## Context

ADR 0002 made *applying the patch* the single Queen-side approval, performed by a
human via `review --approve`. The north-star workloads (U1: the dependency/audit
bot) need that approval to happen *without* a human when it is safe to — but
"safe" has to mean something checkable, and every grant of autonomy has to be
auditable after the fact.

## Decision

Policy gains the power to **grant** autonomy, not only to deny actions:
declarative `AutoApprovalRule`s (`skep/supervisor/policy.py`) that auto-apply a
worker's patch when all enabled conditions hold:

- `require_verification_passed` — the worker reported `passed`;
- `require_reverified` — the supervisor's own re-verification confirmed it
  (G10, ADR 0006) — *autonomy is gated on independent evidence, not the worker's
  word*;
- `forbid_risk_flags` — the result carried no `risk_flags`;
- `diff_scope` — every changed file matches an allowed glob (e.g. lockfiles
  only);
- `max_changed_files` — an optional ceiling.

The first matching rule fires: the patch is applied on `skep/<task_id>` through
the same shared `apply_patch_on_branch` the human path uses, and an approval row
is recorded `resolved_by = auto:<rule>` with a note naming the rule and the
conditions it matched. This sits entirely on top of patch-as-approval — **zero
contract change.**

## Built in v2, active in v3 — dormant by default

`SupervisorConfig.auto_approval_rules` defaults to empty: with no rules, nothing
is ever auto-approved and the human loop is byte-for-byte unchanged (the golden
smoke and reliability gate run with no rules). The mechanism is fully built and
tested now; it is *switched on* for real recurring workloads in v3 (U1), which is
also when the inputs it reasons about (network allowlist, scheduling) arrive.

## Consequences

- The G10 ↔ D3 seam is the safety property: a worker that claims `completed` but
  fails re-verification is **never** auto-approved (proven by a `MODE:liar`
  end-to-end test). Re-verification is not decoration — it is the gate autonomy
  depends on.
- Escalation over silence: a rule that matches but whose patch fails to apply is
  left as a *pending* approval for a human, not dropped.
- Every autonomous action is as auditable as a human one — same approval row,
  same `Approved-by` commit trailer, with `auto:<rule>` instead of a username.
