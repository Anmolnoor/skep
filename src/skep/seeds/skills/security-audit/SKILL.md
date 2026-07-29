---
name: security-audit
description: repo-wide security sweep with anchored findings (adapted from ECC's security-review)
---

# Security audit

Tools: search_files, read_file, git_log, delegate_analysis, dispatch_run, add_note

A whole-repo sweep. For ONE diff before landing, use
requesting-code-review — its checklist is the same, scoped to the hunk.
This is engineering triage, not a compliance certification; say so if
the user asks for the latter.

1. Map the attack surface first: `search_files` for entry points that
   take untrusted input — HTTP routes, CLI args, webhook handlers,
   deserializers, file uploads, LLM/tool boundaries. An audit not
   anchored to a real input path is a checklist recital.
2. Sweep on this spine, one `search_files` pass each:
   - **Secrets** — literals that look like keys/tokens/passwords, and
     `git_log -S` for ones removed from HEAD but still in history.
   - **Input validation** — is every entry point from step 1 validated
     at the boundary, allowlist not denylist?
   - **Injection** — string-built SQL/shell/paths; parameterized or not.
   - **Authz** — is it enforced server-side per request, or assumed
     from a client-supplied field?
   - **Exposure** — secrets in logs, errors, or client bundles;
     stack traces returned to callers.
   - **Dependencies** — lockfile present and committed; known-stale
     pins.
3. Big repo → `delegate_analysis`, one analyst per surface (transport,
   storage, auth), then synthesize. Corners nobody read are listed as
   unread — coverage honesty is part of the report (I8).
4. Every finding: `file:line`, the concrete input that reaches it, and
   the fix. No anchor → label it a hypothesis, not a finding.
5. Rank by reachability, not by category severity: a hardcoded key in a
   published artifact outranks a theoretical XSS behind auth.
6. `add_note` the report. Fixes go out as ordinary `dispatch_run`
   briefs, one issue per run, each with its regression check stated up
   front — never a single "fix all the security issues" dispatch.
