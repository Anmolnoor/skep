---
name: skep-skill-authoring
description: write a good SKILL.md for this registry (adapted from hermes-agent-skill-authoring)
---

# Authoring skep skills

Tools: create_skill, view_skill, list_skills, patch_skill

The SKILL.md format this shelf uses: `---` frontmatter (name,
description, optional worker_kind), then a body of numbered steps.

1. A skill is procedural knowledge, not permissions: name the TOOLS it
   uses in a `Tools:` line (a test cross-checks they exist) and write
   steps a small model can follow verbatim. Chat-authored skills carry
   zero grants by construction.
2. Good skills say when NOT to apply ("multi-page → start_research
   instead") and what honest failure looks like — the reader is the
   Queen mid-task, not a human browsing.
3. One skill = one ask-shape. If the steps fork on "what kind of X",
   write two skills.
4. `create_skill` to save (carded); `view_skill` to check how it reads;
   `patch_skill` to iterate. Repo-shipped seeds live in
   src/skep/seeds/skills/ and follow the same rules (zero-grant,
   ADR 0043).
