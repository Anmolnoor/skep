---
name: audit-an-agent-stack
description: audit an LLM agent's prompt, tools, memory, loop and authority from what its runs actually did (adapted from ECC's agent-architecture-audit)
---

# Audit an agent stack

Tools: search_files, read_file, git_log, get_run, list_runs, delegate_analysis, add_note

Not a threat sweep (`security-audit`) and not a repo map
(`investigate-a-codebase`). The audit that matters is not "read the
prompts" — it is "read what the loop actually did".

1. Find the ASSEMBLY POINT: the function that builds what is really
   sent — system text, tool schemas, replayed history. `search_files`
   for where the messages list is constructed, then `read_file` it.
   Audit the assembled payload, never the template files alone: a stale
   block nobody removed is the commonest defect and is invisible from
   the templates.
2. Tool surface — do the descriptions match the executor? A description
   promising an argument the schema lacks trains the model to
   hallucinate; a tool listed and never dispatchable is worse. Read the
   schema and the dispatch branch side by side, per tool.
3. Context and memory — what persists, what is replayed, what gets
   dropped first when the window fills, and whether that drop is
   MEASURED or assumed. An unmeasured budget is a finding.
4. The loop — is it bounded (iterations, actions, wall clock)? Are
   retries backed off and idempotent? Does a mid-loop failure leave
   state a human can read?
5. Authority — where the model's output becomes an effect, and what
   stands between. One boundary or several? A second permission path is
   a finding, not a design (I5).
6. EVIDENCE: pick one real past run (`list_runs`, then `get_run`) and
   reconstruct it end to end from what the system stored. What you
   cannot reconstruct is the headline finding, not a footnote (I10).
7. Wide stack → `delegate_analysis`, one analyst per layer, then
   synthesize. Layers you did not examine are listed as unexamined —
   an audit that hides its own gaps is worth less than none (I8).
   `add_note` the report, every finding anchored `file:line`.
