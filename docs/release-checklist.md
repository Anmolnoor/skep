# Release Checklist

Run this before tagging or publishing a public Skep release.

## Local Gates

From the repo root:

```sh
UV_CACHE_DIR=.uv-cache scripts/local-release-gates.sh
```

The local gate script expands to the non-account-bound checks:

```sh
UV_CACHE_DIR=.uv-cache make all
UV_CACHE_DIR=.uv-cache make smoke
UV_CACHE_DIR=.uv-cache ./scripts/reliability.sh
UV_CACHE_DIR=.uv-cache scripts/release-hygiene-scan.sh
# On a Linux host with bubblewrap installed:
UV_CACHE_DIR=.uv-cache scripts/linux-sandbox-smoke.sh
UV_CACHE_DIR=.uv-cache uv build
UV_CACHE_DIR=.uv-cache uvx twine check dist/*
UV_CACHE_DIR=.uv-cache scripts/package-install-smoke.sh
SKEP_DOCKER_IMAGE=skep:release-local scripts/docker-image-smoke.sh
# Optional on macOS: prove the Linux bubblewrap gate through the Docker image.
SKEP_DOCKER_IMAGE=skep:linux-sandbox-smoke scripts/linux-sandbox-docker-smoke.sh
```

If a public-image pull hangs in Docker credential lookup, run the Docker gate
with an empty config:

```sh
mkdir -p /tmp/skep-docker-config
DOCKER_CONFIG=/tmp/skep-docker-config \
  SKEP_DOCKER_IMAGE=skep:release-local scripts/docker-image-smoke.sh
DOCKER_CONFIG=/tmp/skep-docker-config \
  SKEP_DOCKER_IMAGE=skep:linux-sandbox-smoke scripts/linux-sandbox-docker-smoke.sh
```

Expected results:

- `make all`: ruff, format check, mypy, and default pytest pass.
- `make smoke`: first-party smoke suite passes.
- `scripts/reliability.sh`: `10/10 PASS` with no leftover worktrees.
- `scripts/linux-sandbox-smoke.sh`: a real bubblewrap run completes with
  `execution_mode=sandbox` on Linux.
- `uv build`: creates `dist/skep-<version>.tar.gz` and
  `dist/skep-<version>-py3-none-any.whl`.
- `twine check`: wheel and sdist pass.
- `scripts/package-install-smoke.sh`: installed wheel exposes a working `skep --version`
  in a disposable virtualenv.
- `scripts/docker-image-smoke.sh`: image builds, serves the UI, rejects
  unauthenticated API status, prints an access token, and accepts authenticated
  status/runs requests.
- `scripts/linux-sandbox-docker-smoke.sh`: the Skep image runs the real
  `scripts/linux-sandbox-smoke.sh` path with Linux bubblewrap available.

## Hygiene Scans

```sh
UV_CACHE_DIR=.uv-cache scripts/release-hygiene-scan.sh

old_names='\bf''cli\b|\bBee''keeper\b|\bbee''keeper\b|foundation ''run'
rg -n "$old_names" \
  README.md docs examples CONTRIBUTING.md Makefile pyproject.toml src/skep \
  tests .github .gitignore Dockerfile Dockerfile.dockerignore docker-compose.yml

personal_paths='/Users/''anmolnoor|/home/''anmolnoor'
rg -n "$personal_paths" \
  README.md docs examples CONTRIBUTING.md Makefile pyproject.toml src/skep \
  tests .github .gitignore Dockerfile Dockerfile.dockerignore docker-compose.yml

git log --all -p | rg -i "key|token|secret|password|api_key"
```

The first two commands should print no matches. The history scan needs human
review: test fixtures and auth implementation code can match, but real secrets
must not.

The script covers the first two scans plus narrow high-signal secret patterns.
Keep the broad history scan as a human review step before making the repo public.

## Documentation

Check that these files exist and are current:

- `README.md`
- `CONTRIBUTING.md`
- `LICENSE`
- `docs/quickstart.md`
- `docs/how-it-works.md`
- `docs/workers.md`
- `docs/sandboxing.md`
- `docs/approvals.md`
- `docs/verification.md`
- `docs/cli-reference.md`
- `docs/configuration.md`
- `docs/demo-gif.md`
- `docs/launch.md`
- `docs/post-launch.md`
- `docs/version-history.md`
- `docs/index.html`
- `docs/site.css`
- `docs/site.js`
- `docs/assets/skep-demo.gif`
- `examples/skep-demo/README.md`
- `scripts/package-install-smoke.sh`

Run a relative-link check before release:

```sh
.venv/bin/python scripts/docs-link-smoke.py
```

## External Release Steps

These require account or hosting access and cannot be proven by local tests:

1. Tag the release (the version in `pyproject.toml`; a stray public `v1.0.0`
   tag predates real releases, so versions continue from it):

   ```sh
   git tag -a v1.0.1 -m "skep v1.0.1"
   git push origin v1.0.1
   ```

2. Confirm GitHub Actions passes on macOS and Linux.
3. Confirm the release workflow publishes to PyPI and creates the GitHub release.
4. Confirm the GHCR image is available if container publishing is enabled.
5. Publish or mirror `examples/skep-demo` as the public `skep-demo` repo.
6. Deploy `docs/` to `https://skep.anmolnoor.com`.
7. Verify DNS, HTTPS, landing-page links, and the demo GIF in a browser.
8. Install from PyPI on a fresh machine and run the quickstart.
9. Test at least one real Claude Code run through the adapter:

   ```sh
   UV_CACHE_DIR=.uv-cache scripts/claude-adapter-smoke.sh
   ```

   The script preflights `claude auth status` and `claude --print` before it
   creates a Skep task. If auth is stale, run `claude auth login`. If the
   default Claude Code model is unavailable, set `SKEP_CLAUDE_CODE_CMD` to a
   working command such as `claude --model <available-model>`.

10. Confirm the GitHub profile is up to date with Skep pinned or linked.
11. Confirm anmolnoor.com links to skep.anmolnoor.com.
12. Confirm consulting inquiries still point to the intended email address.
13. Confirm the phone has the HN app / Reddit app / Twitter installed for
    same-day comment response.
14. Post using `docs/launch.md` only after the install and demo checks pass.
