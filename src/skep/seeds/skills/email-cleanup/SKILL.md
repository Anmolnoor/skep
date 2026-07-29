---
name: email-cleanup
description: bulk inbox analysis and cleanup with reviewable batch cards
---

# Email cleanup

Tools: call_mcp_tool, list_mcp_tools, list_approvals

Bulk inbox work on the first-party mail MCP. Reads flow per the
email-scope policy; every destructive step (delete, archive, move) is
proposed as a BATCH CARD, never per message — forty cards is how an
operator stops reading cards.

1. Analyze first: list/read the target range, group by sender, list
   name, or age; report the groups with counts BEFORE proposing
   anything destructive.
2. Propose one batch card per group: the action, the COUNT, and the
   EXACT message list (sender — subject — date, one line each). The
   count and the list must always agree.
3. At most 50 messages per card. A larger set splits into sequential
   complete cards — "batch 2 of 4" in the header — each fully listed.
   Never summarize, never elide a list item: a truncated batch card
   means the operator approves deletions they never saw.
4. Archive over delete whenever the intent is "get it out of my
   inbox" — reversible beats gone.
5. After each confirmed batch, report what actually happened (n
   archived, n failed) before proposing the next one.
