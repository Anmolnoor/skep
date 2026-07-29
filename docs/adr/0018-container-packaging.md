# ADR 0018 — The box: container packaging and the Linux sandbox posture (v5)

Date: 2026-06-12 · Status: accepted

## Launch update

This ADR documents the container packaging decision before the native Linux
sandbox landed. The current source-install launch path uses bubblewrap for Linux
sandbox mode; the Docker image remains a packaging/trial artifact and is not the
only Linux isolation story.

## Context

The second half of v5: a user runs one command and gets a working supervisor —
no Python, no uv, no git/gh, no sibling checkouts on the host. The serve
daemon (ADR 0017) made this possible; this record fixes how the image is
built, where state lives, and what isolation honestly holds inside a Linux
container (the v4 Q1 sandbox is macOS Seatbelt, which does not exist there).

## Decision

### 1. Build from the Skep checkout; one runtime stage; no node stage

Skep now owns the default worker and its contract, so the image build context is
the Skep checkout itself. One `python:3.12-slim` stage with `git` + `gh` baked
in and one uv-synced environment is enough. Supervisor and worker still stay
separate processes; their boundary is the internal task/event/result contract.
A node build stage does not exist at all — the UI is no-build static files (RFC
decision 1).

### 2. State is one volume

Everything skep writes already lives under `SKEP_HOME`; the image sets it to
`/data/skep`. Mount one named volume at `/data` and the container is fully
disposable: SQLite store, audit evidence, worktrees, cloned repos, model
settings, and the access token all survive restarts. Verified: a restarted
container honors the same token against the same store.

### 3. Publish from CI on tags

The CI `image` job builds from the Skep checkout, boot-checks
the container (index 200, API 401 unauthenticated, token present in logs), and
pushes to GHCR only on `v*` tags. Users pull; they never build.
`docker compose up -d` is the one-command install; the API key and `GH_TOKEN`
ride the environment — skep stores env-var *names*, never secret values (G2).

### 4. The honest Linux sandbox posture

- **The container is the isolation boundary.** Workers run as subprocesses
  inside it; Seatbelt is a no-op off macOS by design (G3).
- **The D1 proxy still enforces the domain allowlist unchanged** — every
  worker gets a per-task loopback filtering proxy.
- **The known gap, stated plainly:** nothing on Linux yet *forces* worker
  egress through that proxy (the iptables egress pin was designed in ADR 0014
  and remains unimplemented). Until it lands, the proxy is advisory for a
  determined worker; the container boundary is the real wall. Documented, not
  hidden.
- **Docker-socket mounting is refused, not deferred by neglect:** letting skep
  spawn sibling worker containers requires `/var/run/docker.sock`, which is
  functionally root on the host — a worse trade than the gap it would close.
  Revisit only with a rootless/sysbox-style design.

## Consequences

- `make image` builds locally from this checkout; the same Dockerfile serves CI.
  Image carries the Skep source tree so the default worker is first-party in the
  container as well as source installs.
- First boot needs no provider: the daemon starts unconfigured and the UI's
  Settings workspace replaces `skep setup --personal` in a terminal.
- The acceptance bar for the box is behavioral: build, boot to a reachable UI,
  401 without the token, token in the logs, and state surviving a restart —
  all checked in CI's boot-check step.
