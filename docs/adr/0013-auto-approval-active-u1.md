# ADR 0013 — D3 auto-approval goes active; the U1 acceptance demo (v3)

Date: 2026-06-11 · Status: accepted

## Context

v2 built the auto-approval **mechanism** (ADR 0007) but left it dormant: with no
rules configured, nothing auto-applies. v3's job is to make it *active* for a real
workload — U1, the nightly dependency/audit bot: "scans a repo, proposes fixes,
auto-lands the safe ones, files the rest." Activating autonomy is the highest-risk
thing this project does, so the activation has to make "safe" mechanical and
narrow, and "the rest" the default.

## Decision

**A built-in `deps-safe` rule, opt-in per dispatch.** `SAFE_DEPENDENCY_RULE`
auto-applies a patch only when *all* hold: the worker's verification passed, the
supervisor re-verified it (G10), there are **no risk flags**, the diff touches
**only** manifest/lockfiles (`requirements*.txt`, `*.lock`, `package-lock.json`,
…), and ≤10 files changed. It is off by default; `skep run --auto-approve` and
`skep tick --auto-approve` switch it on. Dormant-by-default is preserved — you opt
into autonomy, per run.

**"Safe vs the rest" is a property of the fix, surfaced by the worker.** The audit
caste risk-flags a bump that crosses a major version (`major-version-bump:<pkg>`),
because a major bump can break callers in ways a passing test suite won't catch.
`forbid_risk_flags` then blocks auto-approval, so:

- a **minor/patch** bump (verified, re-verified, manifest-only) auto-lands;
- a **major** bump is *filed* — left `completed` with its patch for `skep review`.

**"Land" means open a branch / PR, never push to the default branch** (ADR 0002 +
Stage G). Auto-approval applies the patch on `skep/<task_id>` and records the
approval as `auto:deps-safe`; `--pr` turns that branch into a pull request. A human
or a branch-protection check still merges. The audit trail always names what
granted the autonomy.

## Consequences

- **U1 works end to end, deterministically** (`make u1`): two scheduled audits on
  one tick — the safe one auto-lands (branch + `auto:deps-safe` approval), the
  risky one is filed (no auto-approval, no branch, risk flag recorded). This single
  demo exercises every v3 piece at once: scheduling (E), the audit caste (D2), the
  v0.2 contract, G10 re-verification, and D3 active. It runs offline with no
  provider — the acceptance gate never depends on a live LLM (Q10).
- The safety seam from v2 holds and is now load-bearing: `require_reverified` means
  a worker that lies about passing (caught by G10) can never auto-land; an
  apply failure escalates to pending rather than dropping (ADR 0007).
- Honest limits. "Safe" is *scope + verification + no-major-bump*, not proof the
  new version is behaviour-compatible — the offline demo bumps the manifest and
  re-runs the existing suite; it does not reinstall and test against the new
  version (that needs the Stage C network allowlist, exercised separately). The
  major-version heuristic is deliberately conservative, not a guarantee.
