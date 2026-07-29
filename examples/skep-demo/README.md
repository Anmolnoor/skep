# skep-demo

Small Python project for trying Skep on a safe repository.

## Run tests

```sh
python -m unittest discover -s tests
```

## Try Skep

Try Skep on this repo from a Skep checkout:

```sh
uv run skep run /path/to/skep-demo "add a /health route that returns 200 with {'status': 'ok'}" --execution-mode workspace
uv run skep status --personal
uv run skep review <task_id>
uv run skep review <task_id> --approve
```

The demo starts with a tiny route table and a passing test suite. A good Skep
task adds the `/health` route and updates the tests.
