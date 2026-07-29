# Contributing

Thanks for working on Skep. This project is a local supervisor for coding
agents, so changes should preserve the core safety model: sandbox when requested,
record durable evidence, re-verify claims, and land patches only through review.

## Development Setup

```sh
git clone https://github.com/Anmolnoor/skep.git
cd skep
uv sync --frozen
```

Run the full default gate before opening a pull request:

```sh
make all
```

That runs lint, type checking, and the default test suite. The default suite is
hermetic: no live model, no credentials, no network dependency, and no Docker.

## Focused Checks

Use the smallest relevant check while iterating:

```sh
make lint
make type
uv run pytest tests/supervisor/test_reverify.py
uv run pytest tests/supervisor/test_sandbox_bubblewrap.py
make smoke
```

Use opt-in checks only when the change touches that surface:

```sh
make container
make image
```

`make container` needs Docker. `make image` builds the local container image.

## Coding Standards

- Keep changes surgical and tied to the issue or roadmap item.
- Add or update tests for behavior changes.
- Prefer existing supervisor, worker-contract, and serve APIs over new
  abstractions.
- Keep user-facing docs honest about what is physically enforced, what is policy,
  and what is planned.
- Do not add secrets, local state, screenshots, build output, or machine-specific
  launcher files.

## Security Model Expectations

When changing worker execution, approval, sandboxing, or re-verification, include
tests that prove the failure path:

- sandbox unavailable means the worker does not silently start unsandboxed in
  sandbox mode
- unconfirmed re-verification blocks auto-approval
- denied approvals stop or leave the run pending without granting the action
- workers do not mutate the user's active checkout or default branch directly

## Pull Request Notes

In the PR description, include:

- what changed
- how it was verified
- any safety boundary affected
- any known limits or follow-up work

Keep generated artifacts out of the diff unless they are the actual deliverable.
