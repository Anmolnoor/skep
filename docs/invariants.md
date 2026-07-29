# The invariants — what makes skep skep

Two lists. Part I is the DNA: traits built across v1–v66 that no future
ADR, plan, or refactor may trade away — if a proposed change violates one,
the change is wrong, not the invariant. Part II is the refinement backlog:
directions drawn from a survey of contemporary agent architectures, each
one admitted only because it can be pursued WITHOUT touching Part I.

Every new plan's DoD already carries a "Non-negotiables held" line; this
file is what that line points at. New ADRs should cite the invariant
numbers they touch.

---

## Part I — the invariants

**I1. Landing IS the commit (patch-as-approval).** Completed work exists
only as a patch until a human approval applies it. `main` never advances
automatically; auto-apply exists only on the constrained `skep/`
integration branch in maintain phase (v30). There is no path from worker
output to a default branch that does not pass through a recorded human
verdict.

**I2. The supervisor re-verifies; the worker's word is never the verdict
(G10).** A worker claiming completed+passed is a claim, nothing more. The
supervisor re-runs verification on its own side before the work is
trusted, and the re-verify surface must distinguish "nothing to
re-verify" from "cannot re-verify" honestly (v65). Any change that lets a
worker's self-report become the final verdict is a regression, whatever
it is called.

This includes the *choice of command* (v88-F4). Re-running a command the
worker nominated for itself is still trusting the claim — a worker whose
verify step is `true` earns a confirmed re-verification for a broken
patch. A project's pinned `verify_command` outranks the worker's
nomination, and the record always says which of the two was re-run.
Unpinned projects keep the fallback, so the pin is how a project makes
G10 mean something; a surface that hides which command ran violates
**I8**.

Two places the pin stops being optional (v90). The auto-landing lane —
the only one that lands without a human — will not fire without it: a
worker-nominated command cannot satisfy `require_reverified`. And a
CLI-agent engine may not run without it at all, because its built-in
verification is `git diff --check` (ADR 0047) and re-running whitespace
proves nothing.

The pin is the default, not the exception (v91-F1). Project setup infers
one from the repo's own declared entry point — the same explicit
toolchain table that seeds the shell allowlist — so a project arrives
saying what verification means instead of inheriting the worker's
nomination by accident, and the pin is carried across a phase change
rather than re-derived away. Inference is deliberately conservative: a
pin that cannot pass is indistinguishable from a broken patch, so a repo
with no detectable entry point is pinned to nothing and keeps the
fallback. That fallback is still the weaker guarantee, so every surface
that renders a setup preview says which of the two the project will get,
and `skep doctor` names the projects still on it.

**I3. The Queen thinks; workers act; neither does the other's job.** The
chat brain proposes and reads — every side effect it wants is a card or a
dispatched worker. Workers are disposable, contract-governed, and run in
isolated worktrees. No future convenience (a "quick edit" from chat, a
worker that chats back) may blur this line.

**I4. Workers can NEVER reach a remote or move the branch.** push, pull,
fetch, checkout, switch: hard-denied at the capability layer (v19-F3/F5),
before the verify fast-path and before any allowlist or grant is
consulted, and never persistable as a stored prefix — the denial is
absolute. No allowlist entry, grant, verify label, plugin, or approval
overrides it, and privilege escalation (`sudo`/`doas`) is refused first
precisely because every deny below keys on `argv[0]`. The same list binds
the Queen's own hands (v83-F9): no chat lane may become the git-writing
path workers are denied. The guard list may only ever grow.

Staging and committing are narrower, and the difference is deliberate
(corrected v88-F5 — the earlier wording claimed an absoluteness the code
does not have). `git add`/`git commit` through `shell.run` are
hard-denied on the same footing as the above (v22-F2). The
explicit-intent `git.stage`/`git.commit` capability path is *not*: it is
approval-gated and a resume grant can carry it
(`workers/capabilities.py`). That is safe rather than absolute — the
commit lands in a worktree that is destroyed, and `git.diff` diffs
against the startup baseline so the patch is unaffected either way. What
is absolute is the consequence: no worker-side commit can reach a branch
the operator sees. **I1** is what guarantees that, not I4.

**I5. One authorization boundary; no shadow permission systems.** Every
side effect — filesystem, shell, git, network, ops — passes through the
policy/capability engine. A new feature that carries its own private
allow-logic is rejected on shape alone.

**I6. The model never holds the trigger (ADR 0019).** Confirmation cards
are resolved by humans. Timeouts DENY or supersede, never confirm
(v54-F1, v63-F2). Auto-approval exists only as the per-project trust
ramp the operator explicitly climbs — never a global toggle (v23-F6
deprecated it), never a model decision.

**I7. An explicit operator command is the decision — asked once.** A
typed `/approve <id>` is the confirmation; skep must not ask again
(v51-F0, v63-F1). The mirror rule: an unnamed card is never acted on by
default — EOF, timeout, and silence all read as "skip, never act"
(v50-F1). Both halves are load-bearing; keep them together.

**I8. The record tells the truth, always.** Every terminal state persists
an honest line (v62); a decision taken on one surface reconciles every
other surface's pending question (v63-F2); an alarm may only fire for an
actual failure (v65 — "the majority case rendered as its failure mode"
is a bug class, hunt it). The transcript and audit trail are read by the
model AND the operator: a lie in either poisons both. The audit trail is
the product.

**I9. Errors and rejections teach.** A guard that says only "no" trains
the model to retry broader (v64-F3). Every rejection names what
acceptable looks like; every failure surface carries enough context to
aim the next attempt (v63-F4, v64-F1). Tool descriptions are load-bearing
code — the small model reads nothing else (house rule since the preset
hallucination incident).

**I10. Evolution is forensic.** Field test → reconstruct from the
store/audit trail → plan with observed failure, root cause, and
file:line anchors → one commit per fix → all gates green after every
commit — no pre-existing failures tolerated, ever. A finding that cannot
be anchored in the store is a hypothesis, not a plan item. Live-reproduce
before writing a root cause (the v63 repair-rounds lesson: the code was
right; a stale daemon ran the old code).

**I11. Local-first, operator-owned.** State lives in the operator's home
(`~/.skep`), the server binds locally, the UI is a no-build static app,
and nothing phones home. Tests never touch the operator's real store.
Remote channels (messengers, PR hosts) are outbound conveniences layered
on the local core, never the core.

**I12. The sandbox walls are real and stated.** Workspace-only writes,
allowlisted network, system toolchain — enforced by the sandbox AND
taught to the worker up front (v63-F4, v64-F4), because an unstated wall
produces correct code with dead verification. Verification must succeed
inside the walls or the run honestly fails.

**I13. Approvals are a ledger, not a moment.** Actor, timestamp,
resolution note, landing branch — every verdict is recorded and final
(the single documented exception: an in-flight card superseded by the
very resolution it delivers, v63-F2). Deny paths terminate runs honestly
(v48-F3); nothing pending is silently forgotten.

### The review checklist for a new ADR or plan

1. Which invariants does this touch? Name them by number.
2. Does any step let work reach a branch without a human verdict? (I1)
3. Does any surface start trusting a worker's self-report? (I2)
4. Does the Queen gain a side effect, or a worker gain a voice? (I3)
5. Does any new grant/allowlist/override reach the git guards? (I4)
6. Does the feature carry private permission logic? (I5)
7. Can any timeout, default, or model output confirm anything? (I6, I7)
8. After this change, can any surface show a state that is false —
   stale, alarmist, or flattering? (I8)
9. Do its new errors teach? Are its tool descriptions the manual? (I9)
10. Is the plan anchored in store forensics with a reproduced root
    cause? (I10)

---

## Part II — the refinement backlog

Drawn from a survey of contemporary agent architectures and this
project's own field failures. Ordered by leverage. Each item names the
invariants it must respect.

**R1 (landed v67). Per-repo briefing file (`SKEP.md`).** A repo-authored briefing —
conventions, how to verify here, known walls — injected into the worker
planning prompt alongside the snapshot, and into the Queen's repo
context. Three field runs died on facts a briefing would have stated
("tests need pytest; the sandbox has none; verify with stdlib").
Cheapest fix with the largest expected payoff. (Respects all; extends
I12.)

**R2 (landed v69, ADR 0040). Bounded reactive execution for workers.** Today a worker commits
to a full plan before seeing any output; the field shows plans die at
first contact with the environment, and v19-F7 → v59-F5 → v64-F1 are
repair passes bolted onto that gap one failure class at a time. The
target shape: a bounded act–observe loop where EACH step still passes
the capability gate (I4, I5), the wall-clock/step budgets still bind,
and the audit trail records the trace the loop actually took (I8). The
plan becomes the trace, not the contract. This is the largest
architectural item; it must land without weakening a single guard.

**R3 (landed v67). Guard-message audit.** v64-F3 fixed one rejection; sweep every
deny/error string in the capability engine, shell prefix rules, and
serve actions for the same disease: does it name the acceptable shape?
(I9.) Mechanical, low-risk, high compound value for a small Queen model.

**R4 (landed v67). Tool-description audit.** Every chat tool description should say
when to use it, when NOT to, and the one fact that prevents the known
failure (the v64-F2 pattern — `shell_verify` was undocumented and cost
four approval rounds). Treat descriptions as the Queen's entire manual.
(I9.)

**R5 (landed v72-F3; started v59-F2/v66). Push, don't poll.** Completion, failure, and waiting-on-you states
should reach the operator where they are (chat notification, channel
push) with a call to action — no state transition relies on someone
asking. (I8.)

**R6 (landed v56 — stands corrected v69-F5). Chat context compaction.**
Already built: ADR 0037 — explicit window, budgeted replay, deterministic
per-chat digest with honest markers; the store transcript is never
truncated. The documented trigger remains: an LLM-written digest only if
the field shows the line format losing the thread. This entry originally
under-credited v56; corrected rather than rebuilt. (I8, I10.)

**R7 (landed v67). Schema-validated structured output everywhere.** The v59-F5
validate-and-repair loop, generalized: any surface that asks a model for
structured data validates at the boundary and feeds the error back for
a bounded retry, instead of failing the operation on first malformed
output. (I9, I10.)

**R8 (landed v72-F8: same-worktree crash resume; react half v69-F3/F6: approval resume in place, per-round crash checkpoints salvaged to the audit dir). Resume and checkpoint as first-class.** The resume_checkpoint
plugin covers approval gates; the same idea now covers interrupted and
crashed runs — a supervisor restart offers "continue from step N"
(resume_run / the /resume deck command, v73-F2) instead of a re-dispatch
from zero, on the reaped-run ingest foundation (v59-F10). Landing rules
unchanged (I1).

**R9 (landed — foundation batch_dispatch v51-F5 / await_runs v71-F3; composition record pinned v73-F6, plans/v73). Independent-task fan-out.** For work that decomposes into
independent pieces, dispatch parallel workers in separate worktrees and
land each piece through its own approval. No shared mutable state
between workers; the Queen composes results, it does not merge them
(I3). The end-to-end record is pinned in the suite
(`test_composition_pin.py`): three workers, three worktrees, three
approvals, main never advancing. The ADR 0025 cap stays 3 — raising it
is a policy question for real field demand.

**R10 (landed v67). Verification-first task framing.** Encourage (template, briefing,
prompt) every dispatched task to state its acceptance check up front —
"add X; verify with Y" — so the worker's verify step is chosen by the
task author, not improvised by the model at the end. The field failures
were all improvised verifies. (I2, I12.)

**R11 (landed v67). The ask-list: every prompt becomes a checklist.** When an
operator message — chat or dispatch — carries more than one ask, the
Queen extracts the asks into an explicit numbered list, shows it in the
chat, carries it with the dispatched task, and checks items off as they
resolve. A three-part prompt must never silently lose part two: the
first field test's line-by-line pty feed already showed the Queen
re-asking instead of tracking, and multi-ask prompts are the normal
case, not the edge. The list is part of the record (I8), so a dropped
item is visible, not silent.

**R12 (landed: /btw v67, steering v69). Mid-task interaction: steer the work, or just ask beside it.**
A running task is write-once today; the operator's only moves are wait
or kill. Two channels, kept strictly apart:
(a) *Steering* — a follow-up prompt attaches to the running task and
reaches the worker at its next safe boundary (a plan-step edge or a
recovery replan), through the contract like every other input, and is
recorded in the audit trail (I8). Powers "also rename X while you're in
there" without kill-and-redispatch. Steering is input, not authority —
it confirms nothing and unlocks nothing (I6, I7).
(b) */btw <question>* — a deck command (client-parsed, the v25 pattern;
COMMANDS, executor, and /help move in lockstep) that asks the Queen a
side question WITHOUT touching the running task: the answer renders
alongside, the task's stream and state are never intercepted, and a
/btw turn is read-only by construction — it can propose nothing and
mutate nothing. The split is the point: one channel changes the work
through the contract, the other can never change anything (I3, I5).

---

**Part II is complete (v73).** Every refinement drawn from the 2026
architecture survey is landed or closed — R1 through R12 above each name
the version that landed them. Subsequent rounds are driven by skep's own
field record, not the original survey.

**R13 (landed v100-F5; seeded by v85 / ADR 0045). Pack self-tests.** The
skill-pack trial was a parse-only syntax smoke — honest, but it proved
nothing about behavior. A pack may now declare `self_test:` in its
SKILL.md; the trial extracts the pack at the same `.skep-skill/<pack_id>/`
path a real run uses and runs that command in the forge's sandboxed,
deny-all-egress script lane, and `tested` is gated on that evidence. A
pack declaring nothing still promotes on the smoke and the evidence says
`level: "syntax"` — the gap closes either by running the check or by
saying plainly that none ran. (I2, I12, I8.)
