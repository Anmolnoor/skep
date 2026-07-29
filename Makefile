.PHONY: all lint format type unit smoke test u1 templates skills public-free whole-app-external container image image-smoke linux-sandbox-docker-smoke docs-link-smoke release-hygiene-scan local-release-gates serve scorecard

all: lint type test

lint:
	uv run ruff check .
	uv run ruff format --check src/skep tests scripts

format:
	uv run ruff check --fix .
	uv run ruff format src/skep tests scripts

type:
	uv run mypy

unit:
	uv run pytest -m "not smoke and not container"

# Default smoke: first-party skep paths only.
smoke:
	uv run pytest tests/smoke -m smoke

# The v3 acceptance demo (U1): the nightly dependency/audit bot — scheduled,
# audit caste, G10 re-verified, D3 auto-approval active. Deterministic, offline.
u1:
	uv run pytest tests/smoke/test_u1_acceptance.py -m smoke -v

# The v3.5 acceptance demo: author a workflow template once, then both run it on
# demand AND bind it to a schedule. Deterministic, offline (audit caste).
templates:
	uv run pytest tests/smoke/test_templates_acceptance.py -m smoke -v

# The v4 acceptance demo: from observed successful runs, GENERATE a candidate skill,
# TEST it (G10 gate), a human APPROVES it into the registry, then run AND schedule it
# exactly like a v3.5 template — and prove a failed-test or denied candidate NEVER
# enters the registry. Deterministic, offline (audit caste).
skills:
	uv run pytest tests/smoke/test_skills_acceptance.py -m smoke -v

# The v11 public-free acceptance demo: preview/save the first-party pack,
# confirm zero-cost assumptions, seed templates/schedules, and dispatch once.
public-free:
	uv run pytest tests/smoke/test_public_free_acceptance.py -m smoke -v

# Opt-in whole-app proof against a real external provider — needs OLLAMA_API_KEY,
# a running Docker daemon; never the gate.
whole-app-external:
	@test -n "$$OLLAMA_API_KEY" || (echo "OLLAMA_API_KEY is required" >&2; exit 2)
	SKEP_WHOLE_APP_EXTERNAL=1 uv run pytest tests/external/test_whole_app_external.py -m external_app --override-ini "addopts=" -v

# The v5 acceptance demo: the whole loop over the HTTP API — boot, token auth,
# assign, SSE-stream the events, approve (patch lands on a branch), edit a
# policy.
serve:
	uv run pytest \
		tests/supervisor/test_serve.py \
		tests/supervisor/test_serve_approvals.py \
		tests/supervisor/test_serve_policy.py \
		-q

# The v12 autonomy scorecard: run the deterministic trust-loop suites and emit
# output/scorecard/scorecard.{json,md}. Fails (non-zero) on any verdict drift.
scorecard:
	uv run python scripts/scorecard.py

test:
	uv run pytest

# Opt-in container portability proof (Q1-B) — needs Docker, never the gate.
container:
	SKEP_CONTAINER=1 uv run pytest -m container --override-ini "addopts=" -v

# The box: build the image from this skep checkout.
image:
	docker build -f Dockerfile -t ghcr.io/anmolnoor/skep:dev .

image-smoke:
	scripts/docker-image-smoke.sh

linux-sandbox-docker-smoke:
	scripts/linux-sandbox-docker-smoke.sh

docs-link-smoke:
	uv run python scripts/docs-link-smoke.py

release-hygiene-scan:
	scripts/release-hygiene-scan.sh

local-release-gates:
	scripts/local-release-gates.sh
