# skep documentation

The curated index. Every doc in this directory is listed here (a test pins
that), so if you can't find it below, it doesn't exist yet.

## Start here

- [`quickstart.md`](quickstart.md) — first run in 5 minutes
- [`how-to-use-on-new-machine.md`](how-to-use-on-new-machine.md) — full
  source, serve, and Docker setup notes
- [`demo-gif.md`](demo-gif.md) — the reproducible terminal demo recording

## Concepts

- [`how-it-works.md`](how-it-works.md) — the Queen/worker split, the
  contract, and the life of a task
- [`sandboxing.md`](sandboxing.md) — macOS Seatbelt and Linux bubblewrap,
  and the network allowlist boundary
- [`verification.md`](verification.md) — independent patch re-verification
  (G10)
- [`approvals.md`](approvals.md) — inline approvals and learned command
  templates
- [`browser.md`](browser.md) — browser automation via a browse-scoped MCP
  server (Playwright), reads flow / acts card

## Reference

- [`cli-reference.md`](cli-reference.md) — every command
- [`configuration.md`](configuration.md) — home, worker, policy, and model
  settings
- [`brain.md`](brain.md) — choosing the assistant model (the v72 dial:
  ollama, openai-compat, anthropic)
- [`assistant-tools.md`](assistant-tools.md) — the first-party mail and
  calendar MCP servers (v72), and the registry-then-forge pattern
- [`mobile.md`](mobile.md) — approve from your phone: what a messenger
  can confirm, what stays web-UI-only, what pushes to you
- [`workers.md`](workers.md) — Claude Code, Codex, Aider, and the adapter
  contract
- [`version-history.md`](version-history.md) — per-version changelog
- [`competitive-analysis.md`](competitive-analysis.md) — where skep stands
  in the 2026 agent landscape (the v72 round's seed)

## Releasing

- [`release-checklist.md`](release-checklist.md) — local and external
  release gates
- [`releases/README.md`](releases/README.md) — how a release actually
  happens, what is parked, and the go/no-go runbook
- [`launch.md`](launch.md) — launch post drafts and first-48-hour checklist
- [`post-launch.md`](post-launch.md) — opportunity paths and the first six
  months

## Decisions

- [`invariants.md`](invariants.md) — the traits no change may trade away,
  the review checklist for new ADRs/plans, and the refinement backlog
- [`adr/README.md`](adr/README.md) — the architecture decision record index

## Contributing

- [`../CONTRIBUTING.md`](../CONTRIBUTING.md) — development setup, gates, and
  PR expectations
- [`../SECURITY.md`](../SECURITY.md) — how to report a vulnerability
