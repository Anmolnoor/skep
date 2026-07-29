# How to Use skep on a New Machine

This guide is for getting a fresh machine from zero to a usable `skep`
supervisor. The normal source runtime and local image build are one-repo: the
default worker, dashboard, storage, and Docker build all live in this checkout.

## Prerequisites

- Git.
- Python 3.12 or newer.
- [`uv`](https://docs.astral.sh/uv/getting-started/installation/) (Python package
  manager used by this repo):

  ```sh
  curl -LsSf https://astral.sh/uv/install.sh | sh
  ```

- Docker Desktop or Docker Engine, only if you want the container path.
- An LLM/provider connection for real coding runs. You can configure this in the
  web Settings page after boot. `GH_TOKEN` is optional and only needed for GitHub
  PR actions.

## Run from Source

```sh
git clone https://github.com/Anmolnoor/skep.git
cd skep
uv sync --frozen
export SKEP_HOME="$PWD/.skep-dev"
uv run skep serve --host 127.0.0.1 --port 8765
```

The server prints an access token at startup:

```text
access token: ...
```

Open `http://127.0.0.1:8765`, paste that token, then go to Settings. Configure
the assistant connection, test it, and select the default model. The default
coding worker uses the saved assistant connection when no explicit worker
profile exists.

For a local CLI run:

```sh
uv run skep run /path/to/repo "Fix the failing test" --execution-mode workspace
uv run skep status --personal
uv run skep review <task_id>
uv run skep review <task_id> --approve
```

By default, `skep` dispatches its in-repo coding worker. To use another worker
explicitly, pass `--worker-cmd` or set `SKEP_WORKER_CMD`. See
`docs/workers.md` for the Claude Code adapter.

## Run with Docker

If a published image is available, this is the simplest container path:

```sh
docker run -d --name skep -p 8765:8765 -v skep-data:/data \
  -e GH_TOKEN="$GH_TOKEN" \
  ghcr.io/anmolnoor/skep:latest
docker logs skep
```

Copy the printed access token and open `http://127.0.0.1:8765`. Container state
lives in the `skep-data` volume.

The checked-in `docker-compose.yml` can also use the published image:

```sh
docker compose pull skep
docker compose up -d --no-build
docker compose logs skep
```

Local image builds use this checkout as the build context.

```sh
make image
```

## Optional Quality Gates

The default development gates run from this repo:

```sh
make all
```

The default smoke gate stays inside this repo:

```sh
make smoke
```

## Common Checks

- If the browser rejects requests, use the latest access token printed by the
  running server or container logs.
- If Docker is already bound to `8765`, stop it with `docker compose stop skep`
  or use another source-run port such as `--port 8766`.
- If coding runs cannot reach the model provider, check the Settings connection
  test first. For a host-local provider from Docker on macOS/Windows, use
  `host.docker.internal` instead of `127.0.0.1`.
- If you need a completely clean local source state, stop the server and remove
  `.skep-dev`. For Docker, remove the `skep-data` volume only if you deliberately
  want to delete all supervisor state.
