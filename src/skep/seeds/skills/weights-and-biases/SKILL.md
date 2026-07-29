---
name: weights-and-biases
description: experiment tracking with wandb, offline-first; cloud sync always cards
---

# Weights & Biases

Tools: run_shell, allow_shell_command, allow_fetch_domain, read_url, dispatch_run

Offline mode FIRST (local-first): runs log to `./wandb/` locally and
nothing leaves the machine until the operator says so.

1. One-time read-verb prefix grant: `allow_shell_command wandb
   offline` — it only flips the local mode switch. Never request a
   grant for the bare `wandb` binary: that would silently cover cloud
   operations too.
2. Training scripts run as coding-caste dispatches with
   `WANDB_MODE=offline` in the env; wandb lives in the run workspace
   venv. Metrics are readable locally from the run dir without any
   account.
3. Pushing local results to the cloud IS egress of local data:
   `wandb sync <run-dir>` (and anything online) runs UNGRANTED so
   every sync cards with the exact run dir being uploaded — the
   operator sees precisely what leaves. Never request a grant for
   sync.
4. Reading your cloud dashboards: `read_url` on the granted domain
   (`allow_fetch_domain api.wandb.ai`) with the operator's key in env.
