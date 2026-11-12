# Explaining skep to a human — a guide for agents

[`SKILL.md`](../SKILL.md) teaches an agent to operate skep. This page is for
the other half: a human asks *what is this, why was that blocked, what can
it do* — and the agent answers. Answer from skep's own output, not from
memory: every claim below has a command that shows it.

## The one-sentence version

> skep is a governor for coding agents: the agent works in a throwaway copy
> of your repo under rules you approved, skep checks its work itself, and
> nothing reaches your repo until you approve the patch.

Then stop and let them ask. Three ideas carry almost every follow-up.

## 1. The governor, not the agent

skep does not write code. It decides what an agent may do — Claude Code,
Codex, Aider, a local model, or skep's own worker — and records every
decision. Say it in their terms:

- **The agent is competent and untrusted.** It never touches the real repo.
  It works in a disposable git worktree, often inside a sandbox.
- **Two results must agree.** The agent says its tests passed; skep re-runs
  the verification itself. Either alone is not enough.
- **Landing is the approval.** Finished work is a patch. `skep review
  <task_id>` shows it; `skep review <task_id> --approve` lands it as a commit
  on a new `skep/<task_id>` branch — never the default branch.
- **Some things are never allowed.** A worker cannot push, pull, fetch or
  switch branches, whatever the rules say. That is the floor.

## 2. The locked baseline

During onboarding the human picked a template — `builder` (coding),
`operator` (ops and audits) or `assistant` (least privilege, the default) —
and locked it. Locking stores its hash; skep checks the hash before every
decision and refuses if the file changed underneath it.

- Show them what they locked: `skep policy explain baseline` — an allow /
  ask / deny table, plus the floor rows no layer lifts.
- Show them it is intact: `skep policy baseline status` (exit 1 = tampered),
  `skep policy baseline diff` for what changed.
- Changing it is their act: `skep policy approve --reapprove` shows the diff
  and asks for a typed `yes`.

The useful framing: *the baseline is a ceiling. Project settings, roles and
delegation can only make it stricter.*

## 3. Why was that blocked? — `why` and `explain`

When a run stops on a card or something is refused, run the same question
skep asked:

```sh
skep policy why ~/workspace shell run "git push"
```

Read three fields back to them, in plain words:

- `operator.verdict` — `allow`, `deny`, or `require_approval` (a human
  answers a card).
- `decided_by` / `rule_id` — *which* rule decided: `floor/...` is the
  never-allowed list, a template rule id comes from the baseline,
  `role:<name>` is the role layer, `grant:session/...` / `grant:global/...`
  is a card someone approved with keep, `global/operator-root` is a read
  inside the operator's own roots, `default/unmatched` means no rule
  matched (so it asks).
- `floor` (when present) — the reason in a sentence.

Example: "`git push` is denied by `floor.worker-git-hard-denies`. Workers
never push; when the work is approved, skep can push the landing branch and
open a pull request for you." Then look the rule up in
`skep policy explain baseline` so they can see it in context.

A newly onboarded skep also prints a short why-line under every verdict card;
`skep onboard --quiet` turns those off and `skep onboard --verbose` back on.
`skep policy decisions` is the full ledger of past decisions, newest first.

## 4. Roles

A role is a prompt, a voice and a policy layer that sits *under* the
baseline: `builder`, `operator`, `assistant`, or a custom one
(`skep role list`, `skep role show <name>`). A role can only tighten — if the
baseline says ask and the role says allow, it still asks. One exception the
operator signed (ADR 0059): a role's catch-all ask (`shell-learnable *`,
`shell-gated *`) is a default, not a ceiling — a command you approved with
keep session/always runs again in that role without a card. The role's
denies and named asks still bind. A chat runs in one role; `skep role use
<name>` sets the default.

Say: "The role changes what the assistant is for and narrows what it may do.
It can never give it more than you locked."

## 5. Delegation

When one agent dispatches another — the chat dispatching a worker, or a
manager dispatching a team with `skep team` — the dispatched one holds the
**intersection** of the two: its own role and what its delegator holds. A
rule the delegator does not have counts as *not granted*. The floor sits
above every chain, and a worker can never dispatch.

Show the chain for one action:

```sh
skep policy why ~/workspace shell run "npm test" --role builder --delegated-by queen@operator
```

The answer gains a `delegation` block naming the chain and what each hop
holds. Under a `builder` baseline, the `builder` role allows `npm test`, but
`queen@operator` would itself have to ask, so the worker asks too:
`rule_id: exceeds_delegator`, verdict `require_approval`. Say: "Authority
only shrinks as it is handed down."

## What not to say

- Don't promise an action will be allowed — run `why` and quote it.
- Don't describe a workaround for a deny. The deny is the product working.
- Don't approve, land, re-lock or tag on their behalf to "save them a step".
  Those are their decisions; a typed skep command is not carded, so typing
  it is deciding.
