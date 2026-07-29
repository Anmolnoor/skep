# 0041 — Reasoning-only delegation: delegate_analysis (v83-F7)

## Status

Accepted (planned v83).

## Question

Every delegation in skep spawns a worker: worktree, contract, sandbox,
G10. That is the right cost for work that touches files and exactly the
wrong cost for "read these three runs and tell me which approach is
better" — analysis needs a context window, not a worktree. Hermes's
`delegate_task` covers this shape; skep had no equivalent.

## Decision

`delegate_analysis(tasks[1..3], context?)` — a carded mutation that runs
each task as ONE read-only Queen turn in its own fresh chat
(`source='analysis'`):

- **Read tools only, by construction.** The analyst turn runs on the
  `/btw` machinery (`read_only=True`): mutations refuse, never card.
  Since `delegate_analysis` is itself a mutation, an analyst can never
  nest a delegation — no recursion, no fan-out explosion, without any
  bespoke guard (I5: no new permission logic).
- **The transcript is the record (I8).** Each analyst's full
  conversation persists as an ordinary chat: `list_chats` shows it,
  `search_chats` finds it, `get_chat_messages` replays it. No shadow
  run records.
- **The Queen synthesizes.** Answers return to the proposing chat;
  composition stays with the Queen exactly as ADR 0025 keeps batch
  results (I3: workers/analysts never talk to each other).
- **Cap 3, its own resource class.** The ADR 0025 worker cap and this
  cap are separate dials; raising either is a policy question for real
  field demand, not a code default.
- **Always carded.** There is no repo, hence no project auto-dispatch
  posture to inherit; the card shows the task prompts verbatim. An
  auto-allow lane can be added later behind an explicit operator grant
  if card fatigue shows up in the field — amend this ADR then.

## Consequences

- "Have two analysts compare these designs" costs two provider
  conversations and zero worktrees.
- Analyst turns inherit the turn loop's own bounds (tool-round cap,
  loop nudges, provider retry) — no new budget machinery.
- Sequential execution for now (≤3 bounded turns); parallel provider
  streams are a latency optimization the field hasn't asked for.
