---
name: spike
description: throwaway experiment to answer one question — learn, then delete
---

# Spike

Tools: run_code, dispatch_run, add_note

A spike answers ONE named question ("is the API fast enough?", "does
the library handle X?") with disposable code.

1. State the question and the decision it feeds BEFORE writing anything.
2. Cheapest probe first: `run_code` (fast=true for pure computation;
   the sandboxed worker lane for anything touching files). A spike
   needing a real workspace → `dispatch_run` briefed "EXPERIMENT — do
   not land: report findings as the run summary".
3. Spike code is never landed. The deliverable is the ANSWER:
   `add_note` the finding (question, method, result, decision) so it
   outlives the chat.
4. Timebox: if the spike sprawls, the question was too big — split it.
