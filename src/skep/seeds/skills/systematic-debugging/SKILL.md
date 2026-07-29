---
name: systematic-debugging
description: 4-phase root-cause debugging — reproduce, localize, explain, verify
---

# Systematic debugging

Tools: read_file, search_files, run_code, dispatch_run, get_run, search_chats

For bugs where the quick look failed. Four phases, no skipping:

1. REPRODUCE: get the exact failing command/input. `run_code` or a
   dispatch to confirm it fails the same way for you. No repro → you are
   guessing; say so and gather more.
2. LOCALIZE: bisect the path — logs, `read_file` the suspect layer,
   `search_chats` for whether this failed before and what fixed it.
   Halve the search space each step; write down what each step ruled out.
3. EXPLAIN: state the mechanism ("X returns stale Y when Z races") and
   check it predicts EVERY observed symptom, not just the loud one. A
   cause that explains half the symptoms is half a cause.
4. VERIFY: fix via `dispatch_run` with the reproduction as the verify
   step; `get_run` to quote the supervisor-side result. Then ask: where
   else does this pattern live? (`search_files` the siblings.)
