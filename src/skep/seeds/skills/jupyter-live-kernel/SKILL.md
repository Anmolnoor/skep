---
name: jupyter-live-kernel
description: interactive Python via a Jupyter server skep starts and probes
---

# Jupyter live kernel

Tools: start_process, read_process_log, run_shell, run_code, stop_process

1. For the USER's interactive session: `start_process`
   "jupyter lab --no-browser --ip 127.0.0.1" (non-repo cwd; loopback
   only), `read_process_log` for the tokened URL, hand it to the user.
2. For YOUR own computations, prefer `run_code` (fast=true for pure
   calculation; the sandboxed worker for anything bigger) — a kernel is
   for humans iterating, not for the Queen.
3. Probing a running kernel/server: `run_shell` curl against the
   loopback API (list sessions, kernel status) — never against a remote
   Jupyter.
4. `stop_process` when the user is done; a forgotten kernel with vault
   access is an open door.
