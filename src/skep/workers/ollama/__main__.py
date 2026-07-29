"""Ollama coding worker — the first-party LLM-planning worker, pointed at Ollama.

The v33 adapters (Claude Code, Codex, Aider) shell out to external agent CLIs.
Ollama is an LLM server, not a coding CLI, so its "adapter" is the first-party
LLM-planning coding worker (``coding_minimal``) driven against an Ollama
endpoint. It runs by default with the SAME credentials as the assistant/Queen:
the saved base URL, model, and 0600 daemon secret. No separate setup — if the
operator configured Ollama for the assistant, this worker uses it.

A worker-specific Ollama can be pointed to with ``SKEP_OLLAMA_URL`` +
``SKEP_OLLAMA_MODEL`` (e.g. a larger local coding model than the chat model);
the credential still comes from the shared daemon secret.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from skep.profile import ProviderProfile
from skep.supervisor.serve.llm import resolve_api_key
from skep.workers.coding_minimal import run_coding_task
from skep.workers.llm_plan import WorkerProvider, worker_provider_from_env

WORKER_VERSION = "ollama-worker-0.1.0"
WORKER_CASTE = "coding"

# A worker-specific Ollama endpoint/model (defaults to the assistant's config).
OLLAMA_URL_ENV = "SKEP_OLLAMA_URL"
OLLAMA_MODEL_ENV = "SKEP_OLLAMA_MODEL"


def _ollama_provider() -> WorkerProvider | None:
    """Resolve the Ollama provider: an explicit worker override if set, else the
    saved assistant provider (same endpoint + model + credentials)."""
    home_raw = os.environ.get("SKEP_HOME", "").strip()
    url = os.environ.get(OLLAMA_URL_ENV, "").strip()
    model = os.environ.get(OLLAMA_MODEL_ENV, "").strip()
    if url and model and home_raw:
        # Explicit worker Ollama; credential is still the shared daemon secret.
        supervisor_home = Path(home_raw) / "supervisor"
        return WorkerProvider(
            profile=ProviderProfile(name="ollama", model=model, endpoint=url),
            api_key=resolve_api_key(supervisor_home),
        )
    # Default: whatever the assistant is configured with — same credentials.
    # When the operator uses Ollama for the assistant, that IS Ollama.
    return worker_provider_from_env()


def run_ollama_task(task_path: Path, out_path: Path) -> int:
    return run_coding_task(task_path, out_path, provider_override=_ollama_provider())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the Ollama-backed coding worker under Skep's contract"
    )
    parser.add_argument("--headless", action="store_true", help="run one contract task and exit")
    parser.add_argument("--task-file", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    if not args.headless:
        parser.error("--headless is required")
    return run_ollama_task(args.task_file, args.out)


if __name__ == "__main__":
    raise SystemExit(main())
