---
name: production-readiness
description: ship-or-block triage from local evidence before a release (adapted from ECC's production-audit)
---

# Production readiness

Tools: repo_state, git_log, search_files, read_file, list_runs, delegate_analysis, add_note

For "is this ready to ship", "what breaks in prod", "what did we miss".
Local evidence only — never upload the repo to a third-party scanner
(I11). Skip this for libraries, docs, and scaffolds unless the user
means packaging readiness.

1. Establish the release surface: `repo_state` for branch and dirtiness,
   `git_log` for what is going out since the last tag. A readiness call
   without a defined "what ships" is unanchored.
2. Audit only boundaries that ACTUALLY exist in this repo
   (`search_files` first, then `read_file` — never assert a missing
   webhook handler is unverified when the project has none):
   - **Start** — does it run from a clean checkout with documented
     commands? Are required env vars named and fail-fast, or discovered
     at 3am as a None?
   - **Data** — do migrations run forward, and is there a rollback or
     recovery path? Are writes, jobs, and webhook handlers idempotent
     under retry and duplicate delivery?
   - **Failure** — what happens when a dependency is down: retry,
     degrade, or crash-loop? Is there a health check that proves
     dependencies are reachable, not just that the process is alive?
   - **Observability** — on an incident, what would you read? Errors
     reported, logs structured, the failing path traceable.
   - **Coverage** — `list_runs` for what verification actually passed
     here recently; CI green on the branch that ships.
3. Wide surface → `delegate_analysis` per lens, then synthesize.
4. Deliver a ranked BLOCK list and a SHIP list, each item `file:line`
   with the concrete failure it prevents. "Consider adding tests" is
   not a finding; "the payment retry path double-charges because
   handler X is not idempotent (path:line)" is.
5. State the verdict plainly — ship, ship with named risks accepted, or
   block — and what was not examined. A readiness report that reads as
   uniformly green is usually an unread report (I8).
