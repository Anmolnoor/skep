# 0048 — policy groups: reusable convenience grants, live-composed (v97)

## Status

Accepted (v97-F1).

## Question

The operator asked for modular policy: define a grant bundle once (an npm
API's hosts, the pip/uv env-bootstrap shell tier, an engine choice), name
it, attach it to any number of projects, and edit it in ONE place with
every attached project following. The near-miss was `PolicyPack`
(`packs.py`): hardcoded in Python, bound 1:1 to a strategy, and copied as
a snapshot at setup time — editing a pack changes nothing already set up,
and two projects cannot share an à-la-carte bundle.

Whether to admit a new composition layer into run policy is an ADR-shaped
question because run policy is the authorization boundary (I5): a layer
with its own storage and its own merge rules is exactly where a shadow
permission system could grow.

## Decision

**Live composition, one merge point, convenience keys only.**

- A group is a named dict of policy keys stored under the `policy_groups`
  settings key (builtins `python-bootstrap` / `node-dev` are code-defined
  and merged read-side; an operator edit materializes a stored copy, and
  deleting the copy reverts to the builtin — builtins revert, never
  vanish).
- Projects attach groups via a `policy_groups: [names]` project-policy
  key. Composition happens in `run_policy_for_repo` — the single point
  every dispatch, scheduler probe, and policy view already flows through
  (I5) — between phase defaults and the project overlay:
  supervisor defaults → phase defaults → **groups (attach order)** →
  project overlay → per-dispatch args.
- Merge rules, deliberately boring: list keys
  (`default_network`, `allowed_shell_commands`, `default_env_allowlist`)
  **union**, like trusted roots; scalar keys last-group-wins, and the
  project's own overlay **always** beats any group.
- **`GROUPABLE_POLICY_KEYS` excludes the trust ramp.**
  `auto_apply_verified_patch`, `auto_apply_branch`, `allow_git_mutation`,
  `auto_dispatch_allowed`, `trusted_workspace_roots` can never ride a
  group: the trust ramp is climbed per project, explicitly (I6). A group
  carrying one is refused at write time naming the groupable set (I9).
- Group contents pass the exact validators project policy passes —
  including `dangerous_prefix_reason` on shell prefixes — so the
  never-grantable git/remote denies stay unreachable by construction
  (I4). A group that sets `coding_engine` still hits the v90/v94 guard
  block AFTER composition: unpinned verify still fails closed, external
  engines are still forced into the sandbox (I2/I12).
- A dangling attach (group deleted or renamed out from under a project)
  fails the dispatch closed naming the missing group; deleting a group
  that is still attached anywhere is refused naming the projects — the
  same no-stranding discipline as operator-policy denies.
- Copy-on-write fork: editing a shared group in a project's context can
  fork instead (`fork_from` + `repoint_project` on the same write verb) —
  new group carries the edits, the source stays untouched, the one named
  project repoints, all as ONE carded action (I7).

## Rejected

- **Snapshot-on-attach** (pack behavior): loses the edit-once property
  that motivated the feature. Explicitly rejected by the operator.
- **Groupable trust-ramp keys**: rejected on shape — a reusable bundle
  that flips auto-apply is a lateral privilege copy, not a convenience.
- **Group nesting** (groups of groups): YAGNI until a field test demands
  it.
- **A store table per group**: a handful of dicts is a settings blob, not
  a schema.

## Consequences

Reusable grants stop being copy-paste between project overlays, and the
pip-install/npm friction is one attach instead of N allow cards. The cost
is one more layer to reason about in `run_policy_for_repo` — bounded by
the fact that groups can only widen convenience within already-vetted key
shapes, never touch the ramp, and always lose to the project's own words.
