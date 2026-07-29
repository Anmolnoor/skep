---
name: daily-briefing
description: morning summary of runs, approvals, and anything waiting on the user
---

# Daily briefing

Tools: list_runs, list_approvals, list_chats, list_schedules, list_memory_proposals, propose_schedule

The recipe a `prompt` schedule runs every morning (scheduled turns are
read-only and store-only by design — never fetch the web here).

1. `list_runs` since yesterday: completed / failed / still running.
2. `list_approvals`: everything waiting on the user — lead with this;
   pending approvals block landings.
3. `list_memory_proposals` pending review, and `list_schedules` health
   (anything auto-disabled gets a callout with its reason).
4. Deliver in under 15 lines: "Needs you" first, then "Landed", then
   "Watch". Skip empty sections. To set this up recurring: propose a
   `prompt`-caste schedule from the chat that should receive it.
