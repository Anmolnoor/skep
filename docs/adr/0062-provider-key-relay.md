# ADR 0062 — the provider-key relay: the worker never holds the provider key

**Status:** accepted (operator decision D28), delivered as a strangler behind
the `provider_key_relay` kill switch, **default OFF**.
**Related:** ADR 0011 (network proxy), ADR 0019 (the llm-secret exception to
G2), ADR 0047 (CLI agent engines), ADR 0050 (one verb, three faces), ADR 0051
(provider shelf, per-profile keys), ADR 0059 D3 (V6a `provider_hosts`; this
ADR is its deferred V6b). Invariants touched: I5, I8, I9, I11, I12.

## Question

A sandboxed builtin worker that calls its model provider holds the provider
key. It reads `llm-secret` / `llm-secret-<id>` (the B125 carve-out in
`sandbox.secret_paths`) or the profile's `api_key_env` variable when the
operator put it on the env allowlist. Anything that runs in the worker —
a test the model wrote, an install script, a compromised dependency — can
read the key and send it to any host the run may reach. ADR 0059 deferred
the fix (V6b) because netproxy is a CONNECT tunnel: it cannot add a header
inside TLS.

## Decision

### 1. Transport: a route on the run's existing filtering proxy

The relay is not a new listener. It is a second route on the per-run
`FilteringProxy` (`netproxy.py`) that dispatch already starts for every run
with a domain allowlist, and which every backend already lets the worker
reach:

- Seatbelt pins the worker's egress to `localhost:<proxy port>`.
- bubblewrap (`--unshare-net`) reaches it through `skep.netshim`, which
  presents the same port on the sandbox loopback and bridges to the proxy's
  AF_UNIX door. On `es-r5b-landlock` that door is one of the two
  socket-connect rules (the worker's roots and the proxy bridge), so the
  relay needs no new rule.
- With `es-r2a-network` (D6: loopback closed by default) the proxy port is
  still the one loopback port a run may dial. The relay rides it.

A forward-proxy request is always absolute-form (`POST http://host/...`) or
`CONNECT`. The relay takes the two shapes that are addressed to the proxy
itself: an origin-form request (`POST /api/chat`, the worker dialled the
proxy port directly, as Seatbelt's `NO_PROXY=127.0.0.1` makes it) and an
absolute-form request to `127.0.0.1:<the proxy's own port>` (bubblewrap,
where the worker's client goes through `HTTP_PROXY`). Before this ADR both
were refused (400 / a loop to itself). Nothing that worked before changes.

The worker's provider URL becomes `http://127.0.0.1:<port>` plus the
upstream base URL's own path, so every path rule the clients apply to a base
URL (`openai_style_prefix`'s bare-host `/v1`, ollama's `/api` strip) gives
the same path through the relay as direct. The relay replaces only the
origin: it forwards to the upstream's scheme, host and port, over TLS when
the upstream is `https`, refusing a path outside the upstream base path.

A run whose network is allow-all has no proxy today. With the relay ON it
gets a relay-only proxy (empty allowlist); the worker reaches its port
because allow-all permits loopback. A deny-all run never needs one: a caste
that calls a provider always has `provider_hosts`. A podman allow-all run
is not relayed: podman gives every run its own network namespace, so the
host's loopback is out of reach (a podman domain-list run already falls
back to the native backend); it keeps today's path and its record says so.

Rejected:

- **A unix socket bound into the sandbox.** A second door per run, a second
  Landlock rule, a second path for R4 to rebuild, and Seatbelt would need
  its socket allowed by path. The proxy door already exists on every backend.
- **A new loopback port.** On `es-r2a-network` that is a new hole in the
  loopback wall that has to be named in every profile.
- **TLS interception in the CONNECT tunnel.** A CA in the worker's trust
  store, for every host, to rewrite one header.

### 2. Per-run capability: a token bound to the run and to one upstream

The worker sends a per-run token where its client already sends a key
(`Authorization: Bearer <token>`; `x-api-key: <token>` for anthropic), so the
protocol clients in `serve/llm.py` change in no line. The relay compares it
in constant time, strips every credential header (`authorization`,
`x-api-key`, `api-key`, `x-goog-api-key`, `proxy-authorization`) and adds the
real key in the protocol's header. A request without the token gets 403
and no upstream connection.

The token is `HMAC-SHA256(relay-secret, task_id)`. `relay-secret` is a 0600
file the supervisor creates on first use in the supervisor home; its name
matches `*-secret`, so the sandbox mask list and the secret floor already
hide it from every worker and every Queen read tool. One relay serves one
run: another run's worker holds another token, and a relay is bound to one
upstream origin and base path, so a token cannot reach another provider.
Deriving the token, rather than drawing it at random, means a restarted
supervisor recomputes it without storing it anywhere (section 7).

### 3. Kill switch

`provider_key_relay` (bool, default false), a global knob set like every
other supervisor setting: `PUT /api/policy`, the chat `set_policy` card, the
web UI, and listed in the policy registry. Dispatch reads it from the store
for each run, so `skep run` and serve obey the same switch. OFF is exactly
today's path. Each run's `dispatched` line records which path it used.

### 4. Golden-parity gate

`tests/supervisor/test_provider_relay_parity.py` sends the same model calls
(one per relayed protocol: ollama, openai-compat, anthropic,
openai-responses) direct with the key and through the relay with the token,
against the repo's fake providers, and requires the recorded requests to be
identical apart from the auth header: path, query, every other header (Host,
Connection and Content-Length included) and the JSON body, and the parsed
replies identical. The credential header itself must match too: the worker
sent the token, the provider must get the key in the same header and form.
The gate's first run caught one difference (the relay dropped the worker's
`Connection: keep-alive`); the relay now passes a plain keep-alive/close
through. The default may only flip to ON with this gate and the full suite
green.

### 5. Which providers and engines

- **Builtin worker castes (coding, document, reviewer)** — relayed when ON.
  They resolve their provider through one function
  (`llm_plan.worker_provider_from_env`); the supervisor resolves it with the
  same code (`worker_provider_from_home`) and hands the worker the relay URL,
  the token, the protocol and the model. The worker no longer reads a key
  file, and the sandbox masks them (the B125 carve-out is not applied), and
  the profile's `api_key_env` variable is dropped from the worker env.
- **Not relayed, today's path:**
  - `bedrock`: SigV4 signs the host, path and body with the secret, so a
    relay cannot swap a header; it would have to re-sign, i.e. hold the
    secret key in the signer. Possible later; out of scope.
  - a provider with no key (local ollama): nothing to hide.
  - **claude_code** can be pointed at a base URL (`ANTHROPIC_BASE_URL`) and
    sends `x-api-key` or `Authorization: Bearer`. **codex** can
    (`OPENAI_BASE_URL` / a `model_providers` entry) and speaks the Responses
    API. **aider** can, per provider, through litellm (`OPENAI_API_BASE`,
    `ANTHROPIC_API_BASE`, …), but which provider is in the operator's aider
    config, which skep does not read. **pi** is multi-provider by its own
    config. All four authenticate from their own auth env (ADR 0047, W33)
    and stay on that path this round. `skep doctor` says so in one line.
    Relaying claude_code and codex is a follow-up: the same route with the
    engine's base-URL variable, once each engine's full request set
    (count-tokens, model lists, websocket upgrades) passes the parity gate.

### 6. Streaming, logging, accounting

The relay streams: it forwards the response status and headers (minus
hop-by-hop), then the body as it arrives, closing the connection to end it.
SSE and NDJSON reach the worker chunk by chunk, as through the tunnel.

It logs, per request, one counter (`relayed`, `relay_refused`) on the proxy
next to `allowed` / `denied`. Never the key, the token, a header value or a
body: the run record already keeps the prompts it keeps (I8), and the relay
adds none. Usage accounting is unchanged: the worker still counts tokens
from the stream (`ProviderUsageTally`) and the budget's
`max_provider_calls` still binds it; the relay's request count is a check
on that self-report, not a second budget (I5).

### 7. Without serve, and serve restarting mid-run

The relay lives in the process that runs dispatch and spawns the worker. Under
`skep run` that is the CLI process; under serve it is serve. Both start it
through the same code. No serve is needed.

On main, a serve restart ends the run (recovery kills the worker), and the
relay dies with the proxy. With re-adoption (`es-r4-readopt`, ADR 0060) the
new serve restarts the run's proxy on the recorded port from `task.json`;
the relay comes back with it because nothing about it is stored: the
upstream and key are resolved again from the same configuration, and the
token is recomputed from `relay-secret` and the task id. The window between
the old serve's death and the new proxy's start refuses provider calls, as
it refuses all egress today. When R4 lands, its proxy restart must pass the
relay too; that is one call site.

## Strangler order

1. Relay route and kill switch, default OFF (nothing calls it).
2. The golden-parity gate.
3. The builtin worker uses it when ON.
4. The key is absent from the worker (env and files) when ON, proved by a
   real sandboxed worker reading its own environment.
5. Doctor line.

The old path (key files readable to LLM castes, `api_key_env` passthrough)
stays until the relay has run ON as the default through a dogfood period; it
is deleted only then, which is when this door becomes one-way.

## Consequences

- With the relay ON, a worker's only credential is a token that works on its
  own run's relay, for its own provider, while the run lives.
- `supervisor.sqlite3` stays readable to a sandboxed worker this round (other
  worker code reads settings from it). With the relay ON the worker no longer
  needs it for its provider, so masking it is now possible, as a follow-up.
  `~/.npmrc` is unrelated to the provider key and stays readable.
- Workspace mode has no wall: with the relay ON the key leaves the worker's
  environment, but the key files remain readable to a workspace-mode worker.
- A worker can still spend the operator's quota through the relay, at its own
  provider, within its budget. That was true with the key; what it can no
  longer do is take the key away.
