---
name: council
description: argue a decision from three briefed-to-disagree lenses and return one verdict, not a survey
---

# Council

Tools: delegate_analysis, add_note, ask_clarifying_question

For "should we do X". Not for comparing engines on evidence
(`compare-coding-engines`), reviewing a diff (`review-a-pr`), or
planning execution (`plan`). Deliberately thin: a procedure over
`delegate_analysis`, not a new system.

1. State the decision, what changes if it goes the other way, and
   whether it is REVERSIBLE. A one-way door raises the bar; a
   reversible call that has been argued for a week is its own finding.
   Unclear what is being decided → `ask_clarifying_question` first.
2. `delegate_analysis` with THREE lenses briefed to disagree:
   - **the case FOR** — its strongest version, never a strawman;
   - **the SKEPTIC** — how this fails, what breaks, what it costs to
     run;
   - **the SECOND-ORDER voice** — what it forecloses, and who maintains
     it in six months.
   The analysts are reasoning-only and never see each other (I3), so
   the disagreement is real rather than negotiated.
3. ONE verdict, from you: go · go with named conditions · no-go. "The
   council was split" is not a verdict — restate the strongest opposing
   point in one line and say why it did not win.
4. Bound it: a council reasons over what is already KNOWN. If the
   decision turns on an unknown fact, stop and run `spike` or
   `research-a-topic` first — three confident voices over missing
   evidence is theatre.
5. `add_note` the verdict WITH the losing case, so a later reversal has
   something to read (I8).
