# 0045 — External shelves and the skill-pack ladder (v85)

## Status

Accepted (v85).

## Question

The community ships hundreds of Agent Skills (`SKILL.md` packs, the
`~/.claude/skills/` convention) skep can already parse (v44-F6) and
shelf-load (v83-F12). Two gaps kept them out. First, there was no
operator-visible way to point skep at an external shelf. Second — the
sharper one — a pack that ships *scripts* is not instruction text, it
is a **package**: code someone else wrote that a grant will let run
inside worker sandboxes. The prior admit path
(`skep skill import-md --allow-script --approve`) was one human gate
with no trial, no suspend, no rollback — while forged MCP tools (v71)
already walk the full v17 ladder. Same risk class, weaker gate. What
is the governed shape for third-party packages?

## Decision

**Instruction-only packs stay frictionless.** An external shelf
(`skep skill shelf add ~/.claude/skills`, setting `skill_shelves`,
synced at serve start) loads them under the exact seed rules —
zero-grant, tombstones honored, the operator's copy wins — with
provenance `"external"`. Registering the shelf is the operator's
explicit act; the packs it admits are inert text, dispatchable only
through the normal carded run paths (I5, I6). ADR 0043's zero-grant
rule holds off-repo.

**Script-shipping packs walk the v17 ladder** (`skill_packs.py`):

- Import (`import-md --allow-script`) or an external-shelf sync
  creates a **draft** record — no grants live, nothing in the
  registry, nothing runnable.
- Promotion (`skep skill promote` — the typed command is the human
  action, I7 — or the `promote_skill_pack` card, I6) drives
  draft → sandboxed → tested → reviewed → approved → active with every
  edge through `require_transition`: the gates are enforced by shape,
  the forge precedent.
- The **trial** is a supervisor-side, parse-only syntax smoke
  (`compile()` for Python, `sh -n` for shell, readable otherwise).
  Nothing executes, so no worker dispatch is needed; the supervisor
  produces and stores the evidence itself (I2). The pre-active states
  are structurally inert — a pack has no runnable surface until
  activation writes the template.
- **Activation** snapshots the pack into `<skep home>/skills/<id>/`
  and writes the registry template (provenance `"pack"`) with the
  operator-typed grants, script tokens rewritten onto
  `.skep-skill/<id>/…`. At dispatch, the snapshot is materialized into
  the run workspace at exactly that path — the granted argv and the
  file agree by construction, workspace-only writes (I12), every
  sandbox backend.
- **Suspension removes the template** (registered ⟺ active, I8);
  `rolled_back` is terminal — re-import starts a fresh review. Grants
  are recorded on the record and the template only; the worker
  capability engine remains the sole enforcement point (I5), and the
  I4 git/remote denies are untouched by any pack grant.

## Consequences

- Pointing at a community shelf yields instruction skills live and
  script packs queued as drafts — nothing silent, nothing granted.
- Every third-party package now passes a trial plus a human action
  before it can act, and can be paused or retired in one step.
- The trial's ceiling is honest and, since v100-F5, movable: syntax
  when the pack declares nothing, and the declared `self_test:` command
  — run for real in the forge's sandboxed, deny-all-egress trial lane —
  when it does. The evidence carries `level` ("syntax" | "self_test"),
  so a promotion never reads as behavioural when it was not. R13, named
  in this ADR's own upgrade path, is closed.
- Pack instructions referencing bundled non-script files (e.g.
  `references/*.md`) resolve inside the workspace materialization for
  granted packs; instruction-only packs that lean on bundled files
  remain a known limitation of the zero-grant lane.
