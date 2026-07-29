---
name: huggingface-hub
description: search, inspect, and download Hugging Face models and datasets; uploads always card
---

# Hugging Face Hub

Tools: allow_fetch_domain, read_url, search_web, run_shell, allow_shell_command, dispatch_run

1. Search/inspect: `read_url` on the granted domain
   (`allow_fetch_domain huggingface.co`) — the hub's JSON API
   (`/api/models?search=...`) and model/dataset cards. Read the card
   BEFORE downloading: size, license, gated status.
2. Downloads ride a READ-VERB prefix grant the operator may confirm
   once: `allow_shell_command hf download` — never the bare `hf`
   binary, which would silently auto-allow `hf upload` too. Big
   weights go into a run workspace, not skep's own tree.
3. Uploads are cloud egress of local data: they run UNGRANTED so every
   `hf upload` invocation cards with the full argv (repo, path). That
   card is the mechanism behind "uploads card" — never request a grant
   for it.
4. Gated/private repos need the operator's own token in env (`hf auth`
   already done by the operator) — never in chat.
5. Using a model in code is a coding-caste dispatch; heavy deps live
   in the run workspace venv.
