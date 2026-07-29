# 0046 — Session approval tier for the worker shell gate (v86)

## Status

Accepted (v86).

## Question

The shell-gate lane had two approval scopes: a plain approve covered
one run chain, and "approve & remember" was permanent. The operator's
ask: a plain approve should hold *for the session*, with "always" as
the explicit escalation. What is the session tier's shape, and how
does it avoid becoming a shadow permission system (I5) or a silent
ramp to permanence?

## Decision

Three tiers, one engine:

| Tier | Trigger | Writes | Cleared |
|---|---|---|---|
| once | approve on a guarded-class command | nothing standing (verdict rides the run chain) | — |
| session | plain approve | `session_allowed_shell_commands` | at serve startup |
| always | approve & remember / `allow_shell_command` / presets | `allowed_shell_commands` (+ project policy) | never (operator edits) |

- **One engine (I5).** The session tier is a settings key merged
  read-side in `_shell_allowlist_for` — the single choke point every
  dispatch path (serve, CLI, scheduler) already resolves through. No
  new gate, no second allow-logic.
- **Guard classes never persist.** Remote-git (v19-F3/F4), dangerous
  prefixes, and outbound-content prefixes (ADR 0044) are filtered
  before the session write AND on every read (the v19-F3 read-side
  pin applies to the session key too). For those commands a plain
  approve stays exactly what it was: once.
- **No silent promotion.** `policy_view` exposes the session tier
  under its own key; the durable union writers (remember, presets,
  `allow_shell_command`) read only `allowed_shell_commands`, so a
  session grant can never leak into permanence.
- **Session = serve process lifetime.** Startup clears the key and
  logs what it dropped (I8). Restarting skep is the honest "revoke
  everything I approved today" gesture; per-chat scoping was
  considered and rejected — approvals arrive from the web queue,
  Discord, and the deck, which share no chat identity.
- The resolution note says the scope out loud ("approval held for
  this serve session"), so the ledger records which tier fired (I8,
  I13).

Queen-side mutation cards are deliberately untouched: outbound content
must card every time by mechanism (ADR 0044), and scope-level "always"
already exists as learned rules (`allow_mcp_tool`,
`allow_fetch_domain`, `allow_shell_command`).

## Consequences

- Approving `uv run pytest` once stops the identical re-card for
  every later run until the daemon restarts.
- The trust surface an operator holds in their head shrinks to two
  questions: "is this fine today?" (approve) and "is this fine
  forever?" (remember) — with the guarded classes exempt from both.
