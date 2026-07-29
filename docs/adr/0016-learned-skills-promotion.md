# ADR 0016 — Learned skills: a generated template, gated into the same registry (v4)

Date: 2026-06-11 · Status: accepted

## Context

The decision record's v4 row is the skill registry's full form: **learned/promoted
skills** with a **draft → tested → approved pipeline** for *generated* (not
hand-authored) workflows. v3.5 (ADR 0015) shipped the human-authored floor — a
`WorkflowTemplate` is a Queen-side recipe whose filled instance is a *completely
normal* task. v4 builds the learning loop and the promotion pipeline directly on top
of it: a "learned skill" is a **generated `WorkflowTemplate` candidate** that must
pass governance before it can join the same library.

Two temptations had to be resisted up front, because the prompt and the architecture
both forbid them:

1. **Don't oversell the "learning."** There is no trained model and there are no
   learned weights. Calling this "self-improvement" would be dishonest.
2. **Don't let anything self-promote.** A system that generates *and* deploys its own
   workflows with no gate is exactly the kind of unaccountable autonomy skep exists to
   refuse. The whole project's thesis is "trust precisely because you never have to
   trust it."

## Decision

**The generalizer is a deterministic heuristic; the governance is the product.**

### 1. Generation is heuristic pattern-extraction (`skills.py`)

`generate()` reads the task *shapes* of completed, independently re-verified (G10)
runs — the same evidence bar the rest of the system trusts — and groups them by their
fixed recipe knobs (caste, network/env allowlist, budget) plus word count. Within a
group it single-linkage clusters runs whose instructions differ in only a few word
positions, and turns each cluster into a `{{argN}}`-parameterized template: the
positions that vary across the cluster become parameters; the constant words stay.

This is structure extraction, not semantics. It learns *that* a token varies, never
*what it means* — so it names the slot `arg1`, not `project`. It is fully deterministic
(sorted input, union-find clusters, content-addressed output), so the same store always
yields the same candidates. Guards keep it from emitting garbage: more than
`--max-params` varying slots is rejected as over-general, a cluster with no constant
anchor word is rejected, and identical-repeats are *not* generalized (there is no
variable to extract). When in doubt it generates *nothing* — fail toward silence.

A generated candidate is a `WorkflowTemplate` tagged `provenance="learned"`, identical
in every other respect to a hand-authored one. Its name is content-addressed
(`learned-<caste>-<sig8>`), which makes `propose` idempotent and forecloses a
learn-it-again feedback loop: a recipe already drafted, already in the registry, or
previously rejected is never re-proposed.

### 2. Candidates live *outside* the registry until approved (`store.py`)

A draft/tested candidate sits in its own `skill_candidates` table, **not** in the v3.5
`templates` registry. This makes "a candidate cannot be run or scheduled" a structural
fact, not a runtime check: `run --template` / `schedule add --template` read only
`templates`, so there is no code path by which an unapproved candidate executes.
Approval is the *only* writer into `templates`.

### 3. The pipeline is two gates with teeth (`skill_cmds.py`)

- **`skill test` — the G10 test gate.** A draft is instantiated against a real repo
  and dispatched through the *same* `run_task` spine as any task. It is promoted to
  `tested` only if the run completes **and** the supervisor's own re-verification (G10)
  independently confirms it. A non-passing test is **auto-rejected** (`auto:test-gate`)
  and is terminal — fail-closed. This is exactly the prompt's requirement that "a
  candidate that fails its test NEVER enters the registry," enforced by making
  approval structurally require `tested`.
- **`skill approve` — the human gate.** Only a person can move a `tested` candidate
  into the registry. It refuses anything not `tested`, never clobbers an existing
  template name, and stamps `provenance="learned"`. A candidate **never** self-promotes.
  `skill reject` is the human deny path.

### 4. The unified registry is provenance-tagged but behaviour-blind

An approved skill is inserted into the v3.5 `templates` library and is run/scheduled by
the unchanged `run`/`schedule`/`tick` spine. `provenance` is a *tag* surfaced in
`skill`/`template` views; nothing downstream branches on it. "Nothing downstream should
care which is which" is therefore true by construction — the only difference between a
user template and a learned one is a string column.

### 5. Every decision is audit-recorded

Auto-rejection, human approval, and human rejection each write to the existing approval
queue (`action="promote_skill"`, `resolved_by` = the actor or `auto:test-gate`),
anchored to the evidence run, mirroring patch-approval (D3's `auto:<rule>` convention).
The candidate row additionally carries the decision actor, timestamp, note, and the
test/source evidence. The audit trail always names what granted (or refused) the
promotion and the evidence it passed on.

## Consequences

- **Zero contract change, verified.** A learned skill is still just a template → a
  normal task; the instance round-trips through the contract unchanged. No
  `agent-task-contract` bump, no golden-fixture regen — the contract stays on v0.2.
  v4 is entirely Queen-side, exactly like v3.5.
- **The honest framing is the headline.** The "learning" is the cheap part —
  deterministic generalization a senior engineer would call a heuristic. The substance
  is the governance: a test gate and a human-approval gate, both auditable, with a
  fail-closed default. This is *governed* skill acquisition, not autonomous
  self-improvement, and the docs say so plainly.

## Honest limits (recorded, not papered over)

- **It is not ML.** No training, no weights, no generalization beyond
  word-position alignment. Two runs whose instructions are phrased differently (not just
  one token apart) will not be recognised as the same shape.
- **Parameters are named structurally, not semantically** (`arg1`, not `project`). The
  generalizer cannot know what a slot means. `approve --as` lets a human give the *skill*
  a friendly name, but the params keep their positional names (a semantic-naming pass is
  additive future work).
- **Whitespace is normalized** to word tokens during generalization, so a learned
  recipe's instructions collapse runs of whitespace. Fine for the single-line
  instructions in practice; multi-line recipes would lose exact formatting.
- **Generalization is conservative by design.** Mixed-shape or over-general clusters are
  skipped rather than approximated — the system prefers proposing nothing to proposing
  noise. Tuning lives in `--min-occurrences` / `--max-params`.
- **It learns only from confirmed successes.** A run must be `completed` *and*
  G10-confirmed to feed the generalizer; failed, unverified, or unconfirmed runs are
  invisible to it — the learning inherits the system's existing evidence bar.
- **The test gate's strength is the audit caste's determinism.** For a `coding`-caste
  candidate the test gate still holds (completed + G10-confirmed), but the test run then
  depends on a real worker/provider; the offline guarantee is specific to the
  deterministic audit caste used in the acceptance demo.
