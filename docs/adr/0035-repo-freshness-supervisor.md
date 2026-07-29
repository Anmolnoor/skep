# ADR 0035 — Repo freshness is managed by the supervisor (v55-F1/F2)

Date: 2026-07-18 · Status: accepted

## Context

The worker-side deny reason for remote git is literally
`capability.deny.remote_git_managed_by_supervisor` (v19-F3) — but the
supervisor never manned that station. The only network git operation in
the whole supervisor was the single `git clone` at registration, so every
registered repo's clone was frozen at registration day: remote-tracking
refs never updated, `origin/HEAD` never moved, and the local default
branch (which every dispatch baselines from, v22-F1) rotted forever.
Field-test words: "it works with the new project, but not with old,
already registered one" — new registrations were fresh by accident,
nothing kept them fresh. Asked to fix it, the Queen recommended
allowlisting `git fetch` (impossible: rejected at the action layer and
hard-denied worker-side before any allowlist check) — the missing verb
was supervisor-side, not a policy hole.

The operator's checklist for starting work names the missing step
exactly: *is it a registered project? → is it on the latest code? → then
branch and work.* Skep ran the first step and skipped the second.

## Decision

1. **`refresh_clone` (apply.py): fetch + fast-forward, supervisor-side.**
   `git fetch --prune origin` (bounded by a timeout), then fast-forward
   the default branch to `origin/<default>` when it is checked out and
   strictly behind. A diverged, detached, or dirty clone is reported
   honestly (`fast_forwarded: false` + git's own message), never forced.

2. **Mirroring is not landing.** Fast-forwarding a managed clone's
   default branch to origin is *mirroring upstream state*, not advancing
   `main` with skep-authored work. The invariant "main never advances
   automatically" is about skep's patches — those still land only through
   approvals onto non-default `skep/*` branches (ADR 0002). Keeping the
   mirror current is what makes baselines and landings reflect reality.

3. **`refresh_repo` is a carded operator verb** (chat tool +
   `POST /api/repos/{name}/refresh`), and **dispatch auto-refreshes
   managed clones** before resolving the baseline ref. Auto-refresh
   applies ONLY to clones under `SKEP_HOME/repos` — `workon` directories
   are the operator's own checkouts and are never fetched uninvited. A
   failed refresh (offline, dead remote) logs a warning and dispatch
   proceeds from the clone as-is; freshness must never break offline work.

4. **Nothing changes worker-side.** `git fetch`/`pull`/`push` stay
   hard-denied in workers before any allowlist or grant check; no
   allowlist entry can ever name them.

## Consequences

- Branches and commits pushed after registration are dispatchable with
  no manual `git fetch` in `~/.skep/repos/<slug>`.
- A ref that genuinely doesn't exist now fails against *current* upstream
  state, not a stale clone's view of it.
- The clone's default branch may now advance (to mirror origin) without
  operator action; anyone hand-committing inside a managed clone will see
  fast-forwards stop with an honest report — managed clones are skep's,
  not workspaces, which is why `workon` refuses paths under the store.
