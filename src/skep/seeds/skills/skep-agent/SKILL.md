---
name: skep-agent
description: configure and extend skep itself — policy, schedules, skills, forge
---

# Configuring skep (adapted from hermes-agent)

Tools: effective_policy, get_policy, set_operator_policy, apply_policy_preset, propose_schedule, list_schedules, forge_tool, list_plugins, describe_tools

The self-configuration map — where each kind of "make skep do X" lives:

1. Permissions: `effective_policy` (what a run would actually get) →
   `set_operator_policy` / `apply_policy_preset` / the allow_* grants.
   Deny always wins; the git guards and card system are not
   configurable — do not offer to relax them.
2. Recurring behavior: `propose_schedule` — worker castes for repo
   work, note/script/digest for repo-less ticks, `prompt` for a
   read-only morning Queen turn.
3. New tools: `forge_tool` authors a single-file MCP server → sandboxed
   trial → `promote_tool` (one card). New skills: the create_skill /
   seed shelf lane.
4. Unknown surface? `describe_tools` before improvising — the index in
   the prompt lists everything; guessing tool names wastes turns.
