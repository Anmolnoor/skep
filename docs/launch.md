# Launch Prep

Use this file when the repo, package, landing page, and demo are ready to go
public. Keep links current before posting.

## Links

- Landing page: <https://skep.anmolnoor.com>
- GitHub: <https://github.com/Anmolnoor/skep>
- Quickstart: <https://github.com/Anmolnoor/skep/blob/main/docs/quickstart.md>
- Demo repo seed: [`examples/skep-demo`](../examples/skep-demo/README.md)
- Consulting inquiries: <mailto:anmolnoor59@gmail.com>

## Hacker News

Title:

```text
Show HN: skep - Govern your AI coding agent (sandbox, verify, approve)
```

Body:

```text
skep is a supervisor for AI coding agents. Instead of giving an agent full
access to your machine, skep runs it behind a contract: sandboxed execution,
independent re-verification, patch approval, and a durable audit trail.

The agent works in an isolated worktree, produces a diff and evidence, and
cannot push to your repo. If it needs more permission, skep stops at an inline
approval prompt. If you approve and remember a safe command, similar later runs
can reuse that learned template.

The first public release includes macOS Seatbelt and Linux bubblewrap sandbox
backends, a Claude Code adapter, source install, package/release workflows, and
a short demo GIF.

Landing page: https://skep.anmolnoor.com
GitHub: https://github.com/Anmolnoor/skep
Quickstart: https://github.com/Anmolnoor/skep/blob/main/docs/quickstart.md
```

## Reddit

Use one or two relevant communities only. Do not cross-post broadly.

Title:

```text
skep: sandbox, verify, and approve AI coding-agent work before it lands
```

Body:

```text
I built skep, a local supervisor for AI coding agents. It wraps an agent with
OS-level sandboxing, independent re-verification, patch approval, and an audit
trail, so the agent works in a disposable worktree and you approve evidence
instead of trusting a claim.

The launch build supports macOS Seatbelt, Linux bubblewrap, learned approval
templates, and Claude Code, Codex, and Aider adapters behind the same
worker-contract shape (Aider runs with --no-auto-commit — landing stays the
only commit).

Demo + quickstart: https://skep.anmolnoor.com
Repo: https://github.com/Anmolnoor/skep
```

## Twitter / X

```text
1/ I built skep: a local supervisor for AI coding agents.

It sandboxes the agent, re-verifies its work, and makes you approve a patch
before anything lands.

https://skep.anmolnoor.com
```

```text
2/ The problem: coding agents are useful, but raw access is broad.

They can run shell commands, install packages, touch files outside the intended
repo, or claim tests passed without an independent check.
```

```text
3/ skep puts the agent behind a contract.

Task in. Events out. Patch + evidence at the end. The agent works in an
isolated worktree and never pushes to your main branch.
```

```text
4/ The differentiator is re-verification.

skep applies the agent's patch to a clean copy and re-runs the recorded checks.
Evidence over promises.
```

```text
5/ First run: approve explicitly.

If a worker needs a gated shell command, skep prompts inline. Approve once,
deny, skip, or approve + remember for similar future runs.
```

```text
6/ Launch baseline:

- macOS Seatbelt + Linux bubblewrap
- Claude Code adapter
- learned approval templates
- patch review flow
- durable audit trail
- MIT
```

```text
7/ Try it:

Quickstart: https://github.com/Anmolnoor/skep/blob/main/docs/quickstart.md
GitHub: https://github.com/Anmolnoor/skep
Demo: https://skep.anmolnoor.com
```

## LinkedIn

```text
I built skep, an open-source local supervisor for AI coding agents.

The goal is simple: let coding agents move fast without handing them unchecked
access to your machine or repo. skep wraps the agent with OS-level sandboxing,
independent re-verification, patch approval, and a durable audit trail.

The launch build supports macOS Seatbelt, Linux bubblewrap, learned approval
templates, and a Claude Code adapter. The agent works in an isolated worktree,
produces a diff and evidence, and you decide whether the patch lands.

Demo and quickstart: https://skep.anmolnoor.com
Repo: https://github.com/Anmolnoor/skep
```

## Product Hunt

Name:

```text
skep
```

Tagline:

```text
Govern your AI coding agent.
```

Description:

```text
skep supervises AI coding agents with sandboxed execution, independent
re-verification, patch approval, learned approval templates, and a durable audit
trail. It works locally and wraps agents behind a contract instead of replacing
them.
```

## Minimal Analytics

Keep launch measurement simple:

1. GitHub stars/forks/clones from GitHub Insights.
2. PyPI download stats from PyPI or pypistats.
3. Landing page traffic from a privacy-respecting Cloudflare Analytics beacon.
4. Manual checks for HN, Reddit, Twitter, LinkedIn, and Product Hunt engagement.

## Launch Guardrails

- Do not launch on a Friday or weekend.
- Do not post and disappear; be present for comments and bug reports.
- Do not spam multiple subreddits.
- Do not buy ads for the first open-source launch.
- Do not publish the launch post on Medium; keep durable content on the landing
  page or your own site.

## Comment Answers

Docker:

```text
Docker is a useful isolation layer. skep is the governance loop around the
agent: sandbox, re-verify, approve, and audit. The sandbox is one layer; the
patch approval and clean-copy re-verification are the rest of the system.
```

Cursor sandbox:

```text
Cursor's sandbox is editor-specific. skep is local, CLI-native, and
agent-adapter based. The core claim is not just sandboxing; it is independent
re-verification plus a patch approval flow.
```

Agent Safehouse:

```text
Agent Safehouse focuses on sandboxing. skep adds the rest of the control loop:
worker contract, event evidence, clean-copy re-verification, approvals,
remembered templates, and audit history.
```

License:

```text
MIT — use it, fork it, embed it. A governance tool earns trust by being
inspectable and easy to adopt, not by licensing terms; the audit trail and
re-verification are the guarantees.
```

## First 48 Hours

1. Soft launch the public repo, package, release, landing page, and demo.
2. Publish or mirror the `examples/skep-demo` seed as the public demo repo.
3. Re-test install, quickstart, demo GIF, README links, and landing page links.
4. Post Hacker News first on a Tuesday or Wednesday morning Pacific time.
5. Stay in comments for the first 6 hours.
6. Post Reddit and Twitter after the HN post is live.
7. Post LinkedIn once the technical launch links are stable.
8. Schedule Product Hunt for the same week.
9. Check HN, Reddit, and Twitter every 30 minutes for the first 12 hours.
10. Fix reproducible launch bugs immediately and cut patch releases when needed.
11. Track repeated questions as documentation issues.
12. Ask 2-3 friends or collaborators to try it and star it on day 1.
