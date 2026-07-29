---
name: llama-cpp
description: local GGUF inference — build llama.cpp, run models, serve on localhost
---

# llama.cpp — local GGUF inference

Tools: dispatch_run, get_run, start_process, stop_process, list_processes, read_process_log, read_url, allow_fetch_domain

Everything local: weights on disk, inference on this machine, no
cloud. Build and one-shot runs are dispatches; a persistent server is
a managed background process.

1. Build: a coding-caste dispatch clones and `cmake`-builds llama.cpp
   in its own workspace (GPU flags only if the operator confirms the
   hardware).
2. Weights: GGUF from the granted domain (`allow_fetch_domain
   huggingface.co`) into the workspace — check quantization fits RAM
   before downloading (a 70B Q4 is ~40GB; say the number first).
3. One-shot: `llama-cli -m model.gguf -p "..."` inside the dispatch;
   output is the run artifact.
4. Serving: `start_process` with `llama-server -m model.gguf --port
   8080` — health check via `read_url` on
   `http://127.0.0.1:8080/health` (loopback is local RAM, fine to
   probe). Watch `read_process_log` for load errors; `stop_process`
   when done — a forgotten 40GB server is real RAM.
5. The OpenAI-compatible endpoint at `/v1/chat/completions` is how
   other tools (lm-eval, scripts) talk to it.
