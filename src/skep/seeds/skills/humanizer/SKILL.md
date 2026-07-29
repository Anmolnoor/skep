---
name: humanizer
description: rewrite AI-flavored text so it reads like a person wrote it
---

# Humanizer

Tools: read_file, quick_edit, dispatch_run

Strip the tells, keep the meaning. Works on drafts in chat or files in
a repo (landed via the normal patch approval).

1. Kill the stock moves: "delve", "landscape", "It's important to
   note", "In conclusion", rule-of-three everywhere, em-dash chains,
   every paragraph opening with a hedge. One idea per sentence beats
   three clauses balancing each other.
2. Vary rhythm: mix short sentences with long ones; let one paragraph
   be two lines. Uniform paragraph length is the loudest tell.
3. Commit to claims: "X is slower" not "X may potentially be less
   performant in certain scenarios" — unless the uncertainty is real,
   in which case say what's actually unknown.
4. Keep the author's voice when editing someone's draft: their idioms
   stay, their meaning is never "improved". Show a before/after diff
   for anything longer than a paragraph.
5. Read it aloud (mentally) as the check: anywhere you'd never SAY it,
   rewrite it.
