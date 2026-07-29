# 0044 — Outbound content: verbatim cards, never-grantable prefixes (v84-F4)

## Status

Accepted (v84).

## Question

The phase-2 shelf teaches outbound posting (X/Twitter, Discord
administration, email sends). A wrong post is public and permanent —
the highest-blast-radius side effect on the shelf. The v84 plan
originally said "the instruction IS the safety here"; the plan review
called that out: seeds are instructions, not enforcement. If the
operator ever grants `allow_shell_command xurl`, every post is
auto-allowed and no card renders anything. What mechanism guarantees a
post cannot bypass the card?

## Decision

Two rules, one mechanism:

1. **Cards show verbatim payloads.** Every outbound-content side
   effect — post, DM, email send, Discord message — cards with the
   exact final text (the F4 compose→card→confirm recipe). For shell
   paths, the card's full argv IS the payload; for MCP paths, the
   tool-call arguments are shown on the mcp-scope card.
2. **Outbound-content prefixes are never-grantable.** The
   git-prefix precedent (I4's shape) applied to posting:
   `OUTBOUND_CONTENT_PREFIXES` in ``shell_prefixes.py`` (`xurl` —
   posting rides flags, so no prefix of it is read-only; ``himalaya
   message send`` / ``himalaya template send``) is checked in
   ``dangerous_prefix_reason``: a grant attempt is refused with the
   reason at the persist path — presets, cards, and the
   `allow_shell_command` verb all funnel through the same union
   writer, so there is no second door. The commands still RUN:
   ungranted, every invocation cards, forever. Unlike the git denies
   there is no run-time hard deny — posting is legitimate work; only
   the *standing permission* is refused.

Seeds reinforce (never replace) the mechanism: the social seeds
instruct "never request a shell grant for posting", and the v84 seed
grant-hygiene test pins a mutation-verb denylist (send, upload,
delete, sync, post, push) out of every named grant shelf-wide, plus
the rule that curl/wget are never a write path in any seed.
Credentials ride env or the operator's own 0600 config — never chat;
transcripts persist in the store.

## Consequences

- "Every post cards" is now a property of the permission engine, not
  of model obedience — a seed edit, a confused Queen, or an operator
  habit cannot quietly create a silent posting lane.
- The refusal message teaches: it names the rule and the carded path
  (the v64-F3 lesson — an unexplained refusal reads as a retry
  prompt).
- Read-verb prefixes for the same binaries stay grantable
  (`himalaya envelope list`) — reads are cheap, mutations card.
