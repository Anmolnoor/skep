# ADR 0011 — Network domain-allowlist enforcement via a loopback proxy (D1, v3)

Date: 2026-06-11 · Status: accepted

## Context

D1 evolves `permissions.network` from a bool to a per-task **domain allowlist**
(`["pypi.org", "api.github.com"]`); the contract bump landed in Stage A. The
enforcement was the open question. v2 recorded the gap honestly: macOS Seatbelt
filters network by **IP/port, not DNS name**, so it can express "deny all" or
"allow all" but not "allow only pypi.org". A concrete-domain policy therefore
*raised* in v2 rather than pretend to enforce. U1 (the nightly dependency/audit
bot) needs real, scoped egress — package registries and the GitHub API, nothing
else — so v3 has to actually enforce domains, and ideally without requiring the
container layer to be running.

## Decision

Enforce the allowlist by **composing two boundaries the worker cannot slip**:

```
worker --(only loopback:proxy_port, pinned by Seatbelt)--> FilteringProxy
       --(only allowlisted hostnames)--> the internet
```

1. **The half Seatbelt *can* do** — pin egress. For a concrete domain list the
   profile is `(deny network*)` then `(allow network-outbound (remote ip
   "localhost:<proxy_port>"))`. Verified empirically on this machine: Seatbelt
   enforces the *exact* port (a connection to any other loopback port, or any
   external address, is denied with EPERM). So the worker's **sole** path out is
   that one port.

2. **The half Seatbelt *can't* do** — domain filtering. `netproxy.FilteringProxy`
   is a loopback CONNECT-filtering forward proxy that admits only allowlisted
   hostnames (filtering by the CONNECT target host for HTTPS, or the absolute-form
   request host for plain HTTP) and returns `403` for everything else. No TLS
   interception, so it never sees request bodies. The dispatcher starts one proxy
   per networked task, points the worker's `HTTP(S)_PROXY` env at it, and tears it
   down when the worker exits.

Neither half is bypassable: the worker can't reach the network except through the
proxy (Seatbelt), and the proxy won't reach a host the allowlist omits. A domain
list supplied without a proxy port now raises `SandboxAllowlistUnsupported` — a
misconfiguration, never silent un-enforcement.

## Consequences

- **The v2 gap is closed on macOS, without containers.** Per-domain network is
  enforced today. Proven end-to-end by `test_network_allowlist_enforced_end_to_end`:
  a sandboxed worker reaches an allowlisted origin *only* through the proxy, is
  `403`'d for a non-allowlisted host, and a direct bypass to the origin is denied
  by Seatbelt. The proxy's own logic is proven hermetically in `test_netproxy.py`.
- **Honest scope.** Filtering is by hostname (SNI/CONNECT/Host), not by resolved
  IP, and there is no TLS interception — a worker that pins a different IP to an
  allowlisted hostname, or tunnels over an allowed host, is out of scope (the
  threat model is "scope a cooperative worker's egress," not "contain a hostile
  one that controls DNS"). Off-macOS the Seatbelt pin is absent (G3), so the proxy
  still filters but egress isn't physically pinned — the launch says so, same as
  the existing sandbox degradation.
- **Linux portability (Q1-B containers) is now strictly additive.** The same
  proxy runs unchanged inside a container; the container would supply the egress
  pin that Seatbelt supplies on macOS (drop all egress except the proxy). That is
  the deferred portability story (the containers/portability ADR), not a
  prerequisite for enforcement.
