---
name: serving-llms-vllm
description: vLLM server lifecycle — start, health-check, and stop a local inference server
---

# Serving LLMs with vLLM

Tools: start_process, stop_process, list_processes, read_process_log, read_url, dispatch_run

vLLM is GPU-first high-throughput serving. It lives in its own venv
(a dispatch sets it up), and the server is a managed background
process — never a shell command that outlives the chat invisibly.

1. Setup dispatch: venv + `pip install vllm` in a workspace; confirm
   the GPU exists first (`nvidia-smi` in the dispatch) — vLLM without
   CUDA is mostly a wall, say so honestly and offer llama-cpp instead.
2. Start: `start_process` with `vllm serve <model> --port 8000` (add
   `--max-model-len` when VRAM is tight). Startup takes minutes while
   weights load — follow `read_process_log` until "Uvicorn running".
3. Health: `read_url` on `http://127.0.0.1:8000/health`, then a real
   completion via the OpenAI-compatible `/v1/models` — loopback is
   local RAM, fine to probe (never probed as remote).
4. `list_processes` to see what's serving; `stop_process` when done —
   an idle vLLM server holds the whole GPU.
5. Model weights come from the granted huggingface.co lane (the
   huggingface-hub skill); gated models need the operator's token in
   env.
