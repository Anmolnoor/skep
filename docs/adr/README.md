# Architecture Decision Records

The decisions that shaped skep, in order. Each ADR records the question it
answered (the Q/G/D codes trace back to the original design review) and the
version that landed it.

New ADRs and plans start from [the invariants](../invariants.md) — the
traits no change may trade away (Part I, with the review checklist) and
the refinement backlog (Part II). Cite the invariant numbers a decision
touches.

| ADR | Decision |
|-----|----------|
| [0001](0001-contract-ownership.md) | The contract owns itself (Q2-A) |
| [0002](0002-worktree-patch-approval.md) | Worktree + patch artifact; applying the patch is the approval (Q5-A) |
| [0003](0003-carve-out-boundary.md) | Store carve-out boundary (Q6-A) |
| [0004](0004-licensing.md) | Licensing (G1) |
| [0005](0005-seatbelt-sandbox.md) | Worker runs inside a macOS Seatbelt sandbox (Q1-A, v2) |
| [0006](0006-supervisor-reverification.md) | Supervisor-side re-verification (G10, v2) |
| [0007](0007-auto-approval-rules.md) | Auto-approval policy rules (D3, v2) |
| [0008](0008-suspend-resume.md) | True suspend/resume of pending_approval (Q8, v2) |
| [0009](0009-usage-accounting.md) | Usage accounting (G8, v2) |
| [0010](0010-storage-gate.md) | Storage gate: SQLite-WAL single writer (G4, v3) |
| [0011](0011-network-allowlist-proxy.md) | Network domain-allowlist enforcement via a loopback proxy (D1, v3) |
| [0012](0012-scheduling.md) | Recurring schedules: cron drives a stateless tick (v3) |
| [0013](0013-auto-approval-active-u1.md) | D3 auto-approval goes active; the U1 acceptance demo (v3) |
| [0014](0014-container-portability.md) | Containerized worker isolation for Linux portability (Q1-B/G3, v3) |
| [0015](0015-workflow-templates.md) | Workflow templates: a filled template is just a normal task (v3.5) |
| [0016](0016-learned-skills-promotion.md) | Learned skills: a generated template, gated into the same registry (v4) |
| [0017](0017-serve-api-daemon.md) | `skep serve`: the API daemon, the ticker, and mutable config (v5) |
| [0018](0018-container-packaging.md) | The box: container packaging and the Linux sandbox posture (v5) |
| [0019](0019-llm-chat-in-the-queen.md) | LLM chat in the Queen: the voice, and the gated hands (v6) |
| [0020](0020-notes-tasks-gating.md) | Notes & Tasks gating line (v7 Stage B) |
| [0021](0021-browser-automation-deferred.md) | Interactive browser automation: deferred, with named triggers (v37-F5) |
| [0022](0022-policy-first-unified-schema.md) | Policy-first: one schema, two vocabularies, govern-via-MCP (v40, executing v36) |
| [0023](0023-queen-file-reads.md) | Queen-side file reads as a policy-governed tool surface (v51-F2) |
| [0024](0024-script-worker-run-code.md) | Inline code execution as a governed script worker (v51-F3) |
| [0025](0025-batch-dispatch.md) | Batch dispatch as N independent governed runs (v51-F5) |
| [0026](0026-operator-policy-resolution.md) | Operator-policy resolution: the Queen's standing policy (v52) |
| [0027](0027-chat-context-injection.md) | Chat context injection: memory and the skill index (v53-F2/F7) |
| [0028](0028-profile-persona.md) | Profile-level persona: persona.md (v53-F4) |
| [0029](0029-conversation-skills.md) | Conversation-authored skills: observer + curator (v53-F1) |
| [0030](0030-cron-context-chaining.md) | Cron context chaining (v53-F5) |
| [0031](0031-voice-channel-layer.md) | Voice as a channel-layer capability (v53-F6) |
| [0032](0032-card-auto-deny-timeout.md) | Card auto-deny on timeout (v54-F1) |
| [0033](0033-human-readable-cards.md) | Human-readable confirmation cards (v54-F3) |
| [0034](0034-multi-run-pr-grouping.md) | Multi-run PR grouping (v54-F4) |
| [0035](0035-repo-freshness-supervisor.md) | Repo freshness is managed by the supervisor (v55-F1/F2) |
| [0036](0036-project-policy-copy.md) | Per-project policy copy (v55-F4) |
| [0037](0037-chat-context-budget.md) | Chat context: explicit window, budgeted replay, compaction (v56-F1/F2/F3) |
| [0038](0038-approvals-announce.md) | Approvals announce themselves (v56-F5/F6/F7) |
| [0039](0039-queen-git-surface.md) | The Queen's git surface: reads free, mutations carded (v57) |
| [0040](0040-reactive-worker-execution.md) | Bounded reactive worker execution — the act–observe loop (R2, v69) |
| [0041](0041-reasoning-delegation.md) | Reasoning-only delegation: delegate_analysis, cap 3 (v83-F7) |
| [0042](0042-prompt-schedules.md) | Prompt schedules: read-only, store-reads-only Queen turns (v83-F5) |
| [0043](0043-seed-skills.md) | Seed skills and the zero-grant rule (v83-F12/F13) |
| [0044](0044-outbound-content-cards.md) | Outbound content: verbatim cards, never-grantable prefixes (v84-F4) |
| [0045](0045-skill-pack-lifecycle.md) | External shelves and the skill-pack ladder (v85) |
| [0046](0046-session-approvals.md) | Session approval tier for the worker shell gate (v86) |
| [0047](0047-cli-agent-engines.md) | CLI-agent engines and where their authority boundary is (v90) |
| [0048](0048-policy-groups.md) | Policy groups: reusable convenience grants, live-composed (v97) |
| [0049](0049-caste-registry.md) | The caste roster is a registry, not five dict literals (v101) |
| [0050](0050-one-verb-three-faces.md) | One verb, three faces: the operator's surface is never the narrow half (v104) |
| [0051](0051-provider-shelf.md) | The provider shelf: presets, per-profile keys, no borrowed identity (v108) |
