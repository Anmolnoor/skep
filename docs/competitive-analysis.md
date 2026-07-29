# skep competitive analysis — v71 baseline

## The landscape: 4 categories

| Category | What it is | Who's in it |
|----------|-----------|-------------|
| 1. Terminal pair-programmers | Interactive agent in your checkout | Claude Code, Codex CLI, Antigravity CLI (ex-Gemini), Aider, Goose |
| 2. Autonomous cloud engineers | Task in -> VM run -> PR out | Devin, Jules, Codex cloud, OpenHands Cloud, Claude Code cloud sessions |
| 3. Personal AI daemons | Always-on assistant with chat surfaces, cron, memory | OpenClaw, Hermes (retired) |
| 4. Agent orchestrators/supervisors | Coordinator dispatching worker agents | skep, Devin fleets, Gas Town, Ruflo (ex-claude-flow), Vibe Kanban, Bernstein |

skep is the only tool that straddles 3 + 4: a personal daemon (Discord/Telegram/Slack Queen, schedules, memory) that is also a disciplined coding supervisor. The research's key finding: "OpenClaw has the surfaces but not the coding-supervisor discipline; the orchestrators have dispatch but only web/TUI faces" — nobody else occupies skep's square.

---

## Capability dimensions (the rubric)

1. Auto — autonomy model (interactive -> dispatch -> fire-and-forget)
2. Sbx — sandboxing/isolation of side effects
3. Appr — approval flow and landing discipline
4. Git — worktrees, branches, PR machinery
5. Mem — cross-session memory/learning
6. Schd — scheduling/proactive behavior
7. Chat — messaging surfaces
8. Multi — multi-agent orchestration
9. Ext — extensibility (MCP/skills/plugins)
10. Local — local-first, model freedom (ollama etc.)
11. Self — self-extension (builds its own tools)
12. Model — raw brainpower driving it

---

## The grade table

| Tool | Auto | Sbx | Appr | Git | Mem | Schd | Chat | Multi | Ext | Local | Self | Model | Overall |
|------|------|-----|------|-----|-----|------|------|-------|-----|-------|------|-------|---------|
| skep v71 | A- | A | A+ | A- | B+ | A- | B+ | B | A- | A | A- | C | A- |
| Claude Code | A | A- | B+ | A | A- | A- | B- | A | A | C | A- | A+ | A |
| OpenAI Codex | A | A | B+ | A- | B- | A- | B- | B+ | A- | C | B | A | A- |
| Devin | A | B+ | B | A- | A | A- | B+ | A | B | F | B+ | A- | A- |
| OpenHands | A- | B | C+ | B+ | B- | C- | C+ | B+ | A- | A- | C | A- | B+ |
| Jules/Antigravity | B+ | A- | B+ | B+ | B- | C+ | C | B- | B+ | C- | D | A- | B |
| Goose | B- | D | C | C- | C | B+ | C | C+ | A | A | B- | A- | B- |
| OpenClaw | B | D | D | D | B | A | A+ | C+ | A | A | A- | B+ | B- |
| Aider | C | D- | C- | B | C- | F | C- | F | C- | A | F | A- | C+ |
| Hermes † (as deployed) | C+ | C | D | D | C+ | B+ | B | C | B- | B | C | B | C |
| Gas Town / orchestrators | B+ | D | C | B+ | B | C | D | A | B- | B+ | C | A- | B |

† Hermes graded from the deployed record in plans/v44/README.md; retired, skep reached parity in v44.

---

## Notes on the extremes

- skep's A+ in Appr is not grade inflation. The research swept the whole field for it: patch-as-approval (landing IS the commit, main never auto-advances) exists nowhere else. Supervisor-side re-verification (G10: re-apply patch on a clean worktree, re-run verify, compare exit codes before any auto-approval) has exactly two distant cousins — Claude ultrareview's reproduce-before-report ($5–20 per run, cloud) and Bernstein's "Janitor" stage (niche OSS). skep does it free, locally, on every run.

- OpenClaw's D/D in Sbx/Appr is the field's cautionary tale: full host access in the main session, direct execution with no gate, CVE-2026-25253, hundreds of malicious ClawHub skills, and a formal Microsoft advisory classifying it as "untrusted code execution with persistent credentials." Its A+ chat breadth (30+ platforms) and heartbeat proactivity are real — bolted to the inverse of skep's safety posture.

- skep's C in Model is the honest gap: the Queen is glm-5.2, and your own field records show what that costs (re-asking for context already in the transcript, loops that turned out to be tool-surface gaps, weak personality adherence). Everyone else in the top half runs a frontier model.

---

## Where skep is genuinely better (the moat)

1. **Patch-as-approval landing** — unique in the field. Every other tool lands via plain PR (human reviews a diff GitHub renders) or auto-commit (Aider). Only skep makes the human verdict the mechanism, not a convention.

2. **Supervisor-side re-verification (G10)** — worker "completed+passed" is treated as a claim and independently re-run. Nobody else does this locally/free; not_applicable honesty (v65) beats even ultrareview's reporting.

3. **One policy/capability engine** — default-deny, deny-wins-ties, decided_by on every decision, learned rules that can never promote into denied space. The only mainstream analog (Gemini CLI's TOML policy engine) was killed for consumers in June.

4. **Absolute worker git guards** — workers structurally cannot push/pull/commit/branch-switch, with no override path. Claude Code/Codex agents can push if permissions allow; Gas Town agents push constantly.

5. **Governed self-extension** — the v71 forge (author -> sandboxed no-network trial with mandatory self_test -> human card -> activation-is-MCP-registration) vs OpenClaw's "the agent writes its own skills onto a full-access host."

6. **Signed skills with grant disclosure** — the ClawHub malware wave is exactly the failure skep's import gate (verified/tampered/foreign/unsigned + never auto-run) was built against.

7. **Timeouts deny, never confirm** — card auto-deny, fail-closed channel confirmation, hijack-resistant Discord moderation verbs. No other tool has thought this hard about the approval channel itself being hostile.

8. **The category straddle** — governed dispatch plus Discord/Telegram/Slack/voice/webhooks/schedules. Devin has Slack/Teams but is pure SaaS; orchestrators have no chat face at all.

9. **Per-run domain-level egress** (deny-all default, netns + filtering proxy on Linux since v28) — stricter than Claude Code's sandbox proxy and Codex's network-off toggle, which are binary.

10. **Honest observability** — context meter that tells the truth, push-not-poll run states, audit ledger where approvals record actor/timestamp/branch. Devin's VMs are opaque; OpenClaw leaked users' secrets across DM contexts.

---

## Where skep lags, and what to do

Ranked by grade impact per unit of work:

1. **Queen brainpower (C -> the ceiling on everything).** Your field records already prove the loops/re-asks are model properties, not guard gaps. Two moves: (a) make the Queen's provider/model as swappable as worker adapters already are and A/B a stronger local model (glm-5.2 was chosen, not mandated); (b) fatten the Claude Code worker adapter — it currently only captures git diff; parsing verify commands and tool events would let frontier-model workers do the heavy thinking while skep keeps the governance. That's how you buy an A model grade without surrendering the A+ approval grade.

2. **Fan-out scale (R9, Multi B -> A-).** batch_dispatch caps at 3; Devin runs managed fleets, Gas Town runs 20–30. The invariants backlog already gates composition on the react field record (v69-F7) — finishing R9 is the difference between "has multi-agent" and "is an orchestrator" in this field.

3. **Resume/checkpoint (R8).** Same-worktree crash resume and "continue from step N" are table stakes for long autonomous runs; Devin and OpenHands both survive interruption better than skep today.

4. **Memory automation friction (Mem B+ -> A).** The proposal gate is the right posture for grants, but Devin's Knowledge (auto-suggested, review-to-keep) and Claude Code's auto-memory show the convenience bar. v71-F5's observation class (expiring, grant-free, no proposal needed) is exactly the right lane — widen it: more automatic observation capture from runs/chats, keep the human gate only for durable/grant-bearing memory.

5. **Push-don't-poll sweep (R5).** Partial. OpenClaw's heartbeats and Devin's auto-triage set the proactive bar; skep has the ticker and delivery plumbing — finish the sweep so no state transition waits to be asked about.

6. **Mobile/remote presence.** Codex has QR-paired mobile GA; Claude has cloud sessions on mobile. skep's cheap answer already exists — Discord/Telegram mobile apps are the remote face, and v66 made approvals work there. Document that as the story rather than building an app.

7. **Chat breadth** — deliberately don't chase it. OpenClaw's 30+ platforms is its moat and its attack surface. Discord+Telegram+Slack+web+REPL covers your actual life (the v44 gap list was reconstructed from deployed Hermes usage, not its feature matrix — same discipline applies).

8. **Windows** — sandbox is Seatbelt/bwrap/podman; Codex now has a Windows-native sandbox. Fails closed today, which is correct; note it as a known non-goal or a far-backlog item.

9. **Ecosystem risk in reverse** — skep has no community, but the research's churn list (Gemini CLI dead, Bloop dead, Codex app absorbed) shows the flip side: skep depends on nobody's product decisions. Local-first + sqlite + no-build UI is an anti-churn asset; keep it.

---

## What's next

The immediate to-do is already on the calendar: Stage F closes 2026-07-31 and it's the release bar — 13 days of daily-driver evidence is what turns this comparison's grades from architecture into record. The four backlog items above (Queen model, R9, R8, R5) are the natural v72–v74 arc after it.

One caveat on sources: 2026 moves fast — Aider's stagnation, the Moltbook breach details, and Ruflo's self-reported benchmarks were flagged unverified by the research pass.