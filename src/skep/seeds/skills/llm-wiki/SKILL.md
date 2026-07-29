---
name: llm-wiki
description: explain LLM concepts with Karpathy-style first-principles notes
---

# LLM wiki

Tools: search_web, read_url, allow_fetch_domain

For "explain <LLM concept>" asks (attention, RLHF, quantization,
KV-cache, ...):

1. Explain from first principles FIRST — plainly, with the shapes and
   the one equation that matters, the way Karpathy's notes do: build
   from what the reader knows, no jargon before its definition.
2. Verify anything load-bearing or recent (`search_web` + `read_url` a
   primary source — the original paper or a canonical writeup) and cite
   it. Model/version claims stale fast; date them.
3. End with: the misconception people bring to this concept, and the
   one exercise/experiment that would make it click (often a run_code
   toy computation).
