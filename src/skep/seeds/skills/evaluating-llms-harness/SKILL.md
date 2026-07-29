---
name: evaluating-llms-harness
description: run lm-evaluation-harness benchmarks as long coding-caste dispatches
---

# Evaluating LLMs (lm-eval-harness)

Tools: dispatch_run, await_runs, get_run, list_runs, read_file

Benchmark runs are long, heavy, and reproducible — exactly what a
governed dispatch is for. lm-eval and its deps live in the RUN
workspace venv, never in skep's environment.

1. Scope first: model (a local GGUF/HF path or a served endpoint),
   tasks (start with one small task like `arc_easy` to prove the
   plumbing), and limit (`--limit 50` for a smoke pass before any full
   run).
2. Dispatch a coding-caste run: create a venv, `pip install
   lm-eval`, run `lm_eval --model <backend> --tasks <t> --output_path
   results/` — results JSON is the artifact. `Must include:` the
   output file and one metric name.
3. Long runs: `await_runs` rather than polling chat; a full benchmark
   can take hours — say so up front and suggest the smoke pass first.
4. Report metrics from the results JSON with the harness version and
   exact task list — a benchmark number without its config is noise.
5. Comparing runs: same tasks, same limit, same fewshot — diff the
   configs before trusting a delta.
