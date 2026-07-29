---
name: cli-app-field-testing
description: field-test a CLI/daemon app like an operator, not a unit test
---

# CLI app field testing

Tools: run_shell, start_process, read_process_log, stop_process, dispatch_run, add_note

Field-testing means driving the real binary the way a user would.

1. Daemon-shaped apps: `start_process` to launch (non-repo cwd),
   `read_process_log` to watch startup, then exercise it with one-off
   `run_shell` probes (curl the port, run the client).
2. Script the happy path first, then the rude paths: missing args, bad
   config, double start, kill -TERM mid-work. Note every place the app
   lied about its state or died silently — those are the findings.
3. `stop_process` when done; the process table must end clean.
4. Findings become a field-test note (`add_note`) in the house style:
   observed behavior → expected → severity. Fixes are separate
   dispatches, one per root cause — never patch during the test.
