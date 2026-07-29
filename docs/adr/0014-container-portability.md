# ADR 0014 — Containerized worker isolation for Linux portability (Q1-B / G3, v3)

Date: 2026-06-11 · Status: accepted (seam proven; full egress pin deferred)

## Launch update

This ADR records the earlier container-portability path. The current launch path
uses Linux bubblewrap as the native sandbox backend instead of requiring a
worker container: bubblewrap enforces deny-all network and writable bind roots,
while concrete per-domain allowlists fail closed until the proxy can be pinned
inside an enforceable Linux namespace.

## Context

G3 left portability as a v3-entry decision tied to Q1-B (containers): v1–v2 are
macOS-first, with the physical boundary supplied by Seatbelt (ADR 0005) and the
D1 filtering proxy (ADR 0011). Linux and CI have no Seatbelt. Parallel dispatch
(Stage F) also makes per-worker OS-level isolation more valuable. The question:
how does the worker boundary travel off the dev Mac?

## Decision

**Ordinary launch dispatch uses native host sandbox backends.** macOS uses
Seatbelt. Linux uses bubblewrap for deny-all network and writable bind roots, and
fails closed for concrete domain allowlists until Skep owns an enforceable Linux
proxy namespace path. The earlier container proof remains useful as an optional
portability/trial path, not the Linux launch boundary.

When the optional container path is used, the worker runs inside a container with
only its workspace mounted (rw), and HTTP(S) tooling can be routed through the
same host-side `FilteringProxy`, reached at `host.docker.internal`. The egress
pin for that optional path is still platform-specific:

- **macOS:** Seatbelt denies all egress except the loopback proxy port.
- **Optional container backend:** an iptables egress-drop in the container's
  network namespace (allow only the proxy, drop the rest) is the equivalent pin.

`container.py` is the optional seam — it builds the `docker run` argv (workspace
mount, `--add-host` for the host proxy, proxy env). Running a container is
**opt-in and never the gate** (the acceptance bar must not require Docker, Q10).

## What is proven vs deferred (stated honestly)

**Proven** (`pytest -m container`, `SKEP_CONTAINER=1`, real Docker on this host):
a worker in an `alpine` container reaches an allowlisted host **only** through the
host proxy and is `403`'d for a non-allowlisted host — the D1 enforcement,
verbatim, from inside a container. The workspace mount + proxy-env routing work.

**Deferred** (the heavy infra G4/Q1-B flagged):

- The **egress pin** itself. The proof runs on Docker's default bridge, which
  still permits direct egress — so in-container the proxy is *enforced but not yet
  unbypassable*. The iptables drop in the container netns (the macOS-Seatbelt
  equivalent) is designed here but not implemented; until it lands, the container
  story is "domain filtering identical, bypass-prevention pending." This is the
  honest gap, recorded, not papered over.
- **Container dispatch integration.** Ordinary launch dispatch uses the native
  host sandbox backends: Seatbelt on macOS and bubblewrap on Linux. Wiring
  `build_run_argv` into the spawner remains possible later for an explicit
  container backend, but it is no longer the Linux launch path.
- **Container-backend dispatch.** The launch branch has a GHCR-targeted Docker
  image workflow and Linux/macOS CI, but Docker remains a packaging/trial path.
  The supervisor does not select a container backend for ordinary worker dispatch;
  native bubblewrap is the Linux sandbox backend for launch.

## Consequences

- The portability *mechanism* is real and demonstrated, so the claim "the boundary
  travels" is backed by a passing container run, not aspiration — while the parts
  that remain deferred (egress pin and container-backend dispatch) are named
  exactly.
- Postgres (the other G4 option) stays unbuilt by design (ADR 0010); SQLite-WAL
  single-writer is the v3 store. Containers do not change that.
