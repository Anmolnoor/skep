# 0043 — Seed skills and the zero-grant rule (v83-F12/F13)

## Status

Accepted (planned v83).

## Question

skep had a library with shelves, a card catalog, a checkout system —
and zero books: the skill machinery (registry, learning loop, observer,
forge, SKILL.md import) was complete and the registry empty. Hermes
ships 83 skills; the field gap was content, not architecture. How does
stock content enter a system whose whole posture is "nothing enters
without the operator"?

## Decision

Seeds ship in-repo at ``src/skep/seeds/skills/<name>/SKILL.md`` (the
v44-F6 pack format) and sync into the registry at serve startup and via
``skep skill seed``, with ``provenance='seed'``. Three rules, ordered by
who wins:

1. **Zero-grant only.** A seed is procedural knowledge — instructions
   the Queen can read and follow with the tools the operator already
   governs. A seed shipping scripts or wanting any capability is
   SKIPPED with a message naming the deliberate lane
   (``skep skill import-md --allow-script``). Shelf space is free;
   permissions never are (I5, I6 — the skill_md human grant gate holds
   for seeds exactly as for imports).
2. **The operator wins.** An existing template under a seed's name —
   any provenance, including an edited seed — is never overwritten.
3. **Deletes are durable.** Deleting a seed-provenance skill writes a
   tombstone the loader honors forever; a restart never resurrects
   what the operator removed (I8).

The skill index orders operator-authored skills above the stock shelf,
so seeds can never evict what the operator built from the prompt; the
existing entry cap and overflow line bound the block regardless of
shelf size (the review item 6 measurement rides the F13 tests).

## Consequences

- A fresh install answers "research this company" / "review this PR"
  with a loaded recipe instead of improvisation.
- Every seed names only tools that exist — a lockstep test parses the
  whole shelf against ``TOOL_SPECS`` (the v25 COMMANDS-pin lesson).
- Seeds whose Hermes originals assumed capabilities skep routes
  differently are adapted, and say so in their own text — a seed never
  pretends to a capability skep doesn't route (I8).
