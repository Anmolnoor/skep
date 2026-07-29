# Security policy

skep is a security-posture project: workers run in OS sandboxes
(Seatbelt/bubblewrap) with deny-by-default writes and network, every side
effect passes a policy/capability engine, completed work is independently
re-verified, and nothing lands without a human approval. A hole in any of
those layers is a security bug even if no data was touched.

## Reporting a vulnerability

Email **anmolnoor59@gmail.com** with `[skep security]` in the subject.
Please do not open a public issue for anything exploitable.

Include what you can of:

- the skep version (`skep --version`) and OS/sandbox backend
  (`skep doctor` output)
- the boundary affected: sandbox escape, network-allowlist bypass, worker
  git mutation, approval bypass, landing without approval, secret leakage
- reproduction steps or a failing test

You will get an acknowledgment within a week. Fixes land as ordinary
versioned fixes with a test that proves the failure path stays closed.

## Non-goals

Reports that a *locally configured* policy is permissive (e.g. an operator
who allowlisted `*` for network) are configuration feedback, not
vulnerabilities — the policy engine doing what the operator told it to do
is working as designed.
