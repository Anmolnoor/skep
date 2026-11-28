# ADR 0069 — Sandboxed stdio MCP servers and a per-chat `browse` session pool

**Status:** sections 1 and 2 accepted and implemented in v135 Wave 2a (`2.4.0`); sections 3 and 4 proposed, for v135 Wave 2b. Revision 5 (2026-11-27), after three council rounds (Claude Opus 5.5, GPT-6.2 Sol, GPT-6.1 Terra) and six operator decisions the same day.
**Related:** ADR 0047 (external agents confined by the sandbox), ADR 0050 (one verb, three faces), ADR 0059 (policy v3, one `decide()`), ADR 0068 (strict read confinement), Phase 07 (`forged_lane` runtime confinement and its install lane), `docs/browser.md`.
**Invariants touched:** I3, I5, I6, I8, I9, I11, I12, I13.

## Context

Two gaps remain in the MCP surface at `2.3.0` (`Anmolnoor/skep@e27347f1`):

1. **Operator-registered stdio MCP servers run on the host with no sandbox.** They are started with `subprocess.Popen` and no `env=` (`supervisor/mcp_client.py:597`), so they inherit serve's whole environment, including what `serve.env` loaded. ADR 0068 names this as an open hole and `skep doctor` prints it.
2. **`scope=browse` loses its page on every call.** `runner_for_config()` (`supervisor/mcp_client.py:408`) starts one process per exchange (`docs/browser.md:52-55`).

The fast lane and `diagnose_run` deferrals that an earlier revision listed here are closed by v135 Wave 1 and are no longer part of this ADR.

### What the council found in revision 2

All three members agreed on these, with file and line evidence:

- **"Strict by default" would not run the servers this is for.** `setup_browser` starts `npx @playwright/mcp@latest` (`serve/tools.py:5101`), which resolves `@latest` against the registry on every start; the wall sets `HOME` to the workspace with deny-all egress (`supervisor/wall.py:91-95`); npm, uv and Playwright caches under the real home are outside the read wall (`supervisor/sandbox.py:205-214`).
- **A strict browser could not browse.** Strict egress is deny-all or a list of concrete hosts (`wall.py:51-56`, `netproxy.py:131-138`).
- **"Refuse an approved act whose session expired" had no mechanism.** A card carries only model-supplied `server_id`, `tool`, `arguments`; the executor reloads the registration by id at confirm (`serve/tools.py:5882-5887`).
- **A server could run before its HIGH card was answered.** `setup_browser` saves, then runs discovery, which starts the server (`serve/tools.py:5108`); startup also warms registrations (`mcp_client.py:796-833`).
- **`env_allowlist` could hand over credentials**, and `build_worker_env` passes the real `HOME` (`supervisor/spawner.py:67-83`).
- **The pool was under-specified:** fixed JSON-RPC ids and no `initialized` notification (`mcp_client.py:550-583`), no process group (`:597-604`), a card confirm can run while a turn runs, `/stop` does nothing when no turn is running (`serve/chat.py:653-661`, `:4801-4806`), and the ticker must never wait (`serve/ticker.py:353-360`).
- **`server_id` becomes a directory name and is not validated** (`mcp_client.py:52-65`).
- **The wall's profile is written inside the workspace** (`wall.py:62`); a reusable, server-writable workspace would let a server rewrite its own profile.

## Decision

A new module, `src/skep/supervisor/mcp_sandbox.py`, holds the launcher, the install lane and the pool. The strict lane **extends the one shared wall** (`supervisor/wall.py`, `supervisor/sandbox.py`) rather than writing a second one, so forged tools, scripts and MCP servers stay behind the same code. `actions.py` and `tools.py` get wiring only.

### 1. Registration is the authority

1. **Fields on `MCPServerConfig`:**
   - `sandbox: Literal["strict", "host"]`.
   - `egress: Literal["none", "hosts", "open"]` with `allowed_hosts: tuple[str, ...]`. `open` is allowed only when `scope == "browse"` (section 3).
   - `read_paths: tuple[str, ...]` (validated with `sandbox.extra_read_path_problem`, resolved, secret floor masked last).
   - `secret_names: tuple[str, ...]` (names only; section 2.5).
   - `install: tuple[str, ...]` (the pinned package spec, section 2.2).
   - `incarnation`: a supervisor-issued value that changes on **every** save of the registration, including a remove and re-register with identical settings. A digest of the settings is not enough, because a replaced server with the same settings must not inherit an old approval.
   - `server_id` must match a slug pattern wherever it becomes a path, and every directory derived from it is created without following links and checked to resolve inside its parent. Stored `host` registrations with older ids keep loading.
2. **Two defaults, in two places, on purpose.** A new registration that does not name `sandbox` gets `strict`. A stored registration from before `2.4.0` has no field and loads as `host`. A test pins each direction.
3. **The card comes before any launch.** A registration card (and `setup_browser`'s card) shows the effective grant, built without starting anything: the argv that will run (the installed entry point, not the installer), mode, egress, hosts, read paths, secret names and install spec, and the sentence that the install does not run package scripts. Install and discovery run only after the verdict. Startup warming uses only committed registrations and never acquires a pooled session. Re-registering an id, or changing any confinement field, is a new card.
4. **Confinement fields change only through the registration gate.** Registration changes are serialized against validation and dispatch. No tool call, and nothing a server returns, can change them. Sandbox fields on an HTTP-transport server are refused as meaningless. The `forge-` prefix check stays ahead of transport selection.
5. **`sandbox="host"` is a carded delegation of the operator's authority (`I6`, `I8`).** The card says the server runs unsandboxed, can read the files your user can read, can reach the network, and can read skep's own serve token, so it could answer its own cards. The card discloses that; it does not prevent it. `skep doctor` says the same per server.
6. **Every MCP act card is bound to the registration it was proposed for.** At proposal the supervisor (not the model) writes the registration's `incarnation` into the card's `card_json` on the `chat_actions` row; a model-supplied key of that name is dropped; it is part of card de-duplication. At confirm a different `incarnation` refuses, and the result is recorded as **not run**. The final `decide()` call passes `chat_id`.
7. **Typed outcomes reach the ledger (`I8`, `I13`).** Today the MCP client flattens transport errors and the card wrapper can report a failed call as confirmed. Both confirm paths now carry three distinct results: ran, **not run** (refused before sending), **outcome unknown** (the request was sent and the child died or timed out; never retried, never reported as failed).
8. **Faces (ADR 0050).** No new Queen tool. `register_mcp_server`, `unregister_mcp_server` and `setup_browser` are chat-only by a recorded exception (`tests/supervisor/test_surface_parity.py:131-137`). The new parameters ride them. `skep doctor` names the exact chat request that hardens a server.

### 2. The strict lane (Wave 2a)

1. **The shared wall gets four parameters** (`wall.walled`, `sandbox.write_profile`): a supervisor-owned `profile_dir` outside every child-writable root (today the profile is written into the workspace, `wall.py:79-80`); a `home` separate from the workspace, so scratch is writable and the install tree is not; and supervisor-only `trusted_read_roots` for the one server's install tree, which skips the operator-path validation (`extra_read_path_problem` refuses anything under the skep home, `sandbox.py:432`). The fourth is a supervisor-only switch to leave out the roots strict mode grants automatically (`profile.json`, `worker_plugins`, the forged-tool environments, `sandbox.py:375`), which an MCP server and its installer have no use for; the secret masks stay. Existing callers pass none of the four and behave as before.
2. **Install once, under the card, without running package code (operator decision: skep installs).** After the verdict, a pinned spec is installed into a staging tree by `npm install --ignore-scripts` or `uv` with `--only-binary :all:`, inside its own strict wall: scrubbed environment, toolchain reads only, private scratch, only the staging tree writable, egress to the package registry hosts only, **no** `read_paths` and **no** secrets. `@latest` and unpinned specs are refused with the pin to use. The tree is published under a name that includes the `incarnation` only after install and the discovery handshake both succeed; a failure leaves nothing published. The registration's argv is the installed entry point, invoked directly (never `npx` or `uvx` at run time). The install is bounded in time and runs off the request path; its result is reported to the chat. A pre-installed absolute entry point is also accepted and skips the install.
   - **This is weaker than the forged-tool install, and says so.** Forged tools install hash-locked wheels with no dependencies (`forge.py:758-765`). Here only the top package is pinned; its dependencies are resolved when the install runs. A package that needs an install script or a source build does not install in this lane; the error says so and names the `host` or pre-installed alternatives.
3. **Run-time wall.** Strict read allowlist (`strict_read_roots()`, `read_paths`, the published tree as a trusted read root), a private writable `HOME`, temp and cache in per-launch scratch, the registration's egress through `FilteringProxy`, the package managers set offline, and toolchain paths that moving `HOME` hides passed explicitly and read-only (`UV_PYTHON_INSTALL_DIR`; `PLAYWRIGHT_BROWSERS_PATH` waits for 2b, see "As built").
4. **A compliant handshake.** `initialize`, then `notifications/initialized`, then the request, with reply matching by id. The one-shot client gets this in 2a, because real servers are in its acceptance tests.
5. **Secrets come from a per-server file, never from serve's environment (operator decision).** Nothing is forwarded from `os.environ`. A registration lists `secret_names`. The operator sets each value with a CLI command that reads it from a prompt or stdin (`skep mcp secret set <server_id> <NAME>`), so a value never passes through a chat or a card. Values live in a per-server file under the skep home, mode 0600, on the secret floor for every other sandbox, and are injected only into that server's environment. Names the wall sets itself (`HOME`, `PATH`, `TMPDIR`, `npm_config_*`, `UV_*`, proxy variables) and loader controls (`LD_*`, `DYLD_*`, `NODE_OPTIONS`, `NODE_PATH`, `PYTHONPATH`, `PYTHONSTARTUP`) are refused as secret names. A listed name with no value set is a teaching error at the handshake. Changing the list of names is a new card; setting a value is the operator's own CLI act. **What skep enforces and what it cannot:** skep redacts a server's own secret values from that server's tool results, errors and diagnostics before they reach a transcript, a card or a log. It cannot stop a server that was given a secret from disclosing it on purpose, for example by encoding it or sending it to a host it may reach; the card says so.
6. **One host validator** at registration and at launch: concrete hosts only in this release. `NODE_USE_ENV_PROXY=1` is set; when a listed host fails, the handshake error and `skep doctor` report the Node version, since older Node ignores the proxy.
7. **Fail closed (`I12`).** No usable native sandbox backend means the call is refused with an error that says what to install (`I9`).
8. **`setup_browser` in 2a.** Until 2b lands, a sandboxed browser cannot browse (no open egress yet), so `setup_browser` keeps proposing `host`, now as a HIGH card that nothing starts before, and `@latest` stays allowed only for `host`.

### 3. Browsing from inside the wall (Wave 2b)

**Operator decision: open web, files confined.** A `scope="browse"` server may be registered `sandbox="strict"` with `egress="open"`, on a HIGH card; `setup_browser` proposes it by default from 2b.

1. **Open egress is still proxy-only.** It uses the same proxy-pinned sandbox mode as a host list (`--unshare-net` plus the netshim on bubblewrap; "deny network except the proxy port" on Seatbelt) with a proxy allowlist of `*`. It never uses `ALLOW_ALL_NETWORK`, which drops the network namespace on bubblewrap and allows direct connections on Seatbelt (`sandbox.py:1104-1109`, `:1175`).
2. **The proxy refuses non-public destinations for an open list,** for IP literals as well as DNS answers (`netproxy.py:195-208`, `:315`): loopback, private ranges, link-local and the metadata address. So the serve port and the local network stay unreachable.
3. **The browser is told to use the proxy** (`--proxy-server`) and runs `--headless`; the wall has no display socket.
4. **Where Chromium comes from is stated.** The install lane reaches registry hosts only, so it does not download a browser. The spike records, per platform, which pre-provisioned binary the wall can read (Linux system Chrome under `/opt` is readable, `sandbox.py:196`; macOS `/Applications` and the Playwright cache are not, `sandbox.py:561-564`) and the error names the missing step.
5. **Measured first.** Before any pool code, a spike runs pinned Playwright MCP under this wall on real bubblewrap and on real Seatbelt: a public navigate then snapshot must work, and direct egress, a private address, the serve port and an abstract socket must be refused. The result is written into the ADR copy. **Where it fails, that platform has no sandboxed browser in `2.4.0`**, and its documentation and `skep doctor` say so.

### 4. The session pool (Wave 2b)

1. **What is pooled: a sandboxed browser only (operator decision).** A managed Playwright MCP child for one `(chat_id, server_id, incarnation)`, started with an in-memory profile (`--isolated`). **Login state does not persist** (operator decision): cookies last between calls in a live session and end with it. Output files a session writes are kept under `~/.skep/browser/<encoded chat id>/<server_id>/` (a bounded, collision-safe encoding) and deleted on chat delete. A `host` browser is never pooled in this release; it stays one process per call. A scheduled run never acquires a session, including a schedule bound to the same chat: ADR 0042 already refuses MCP calls on an unattended prompt turn, and a test pins it.
2. **A session layer.** One handshake per child, increasing request ids, replies matched by id, server notifications ignored, bounded output, stderr drained, one exchange at a time per session (a card confirm can run while a turn runs). Discovery stays one-shot and separate from session acquisition.
3. **Bounds.** Idle limit 300 seconds, fixed in this release; at most 2 live sessions per serve, with atomic reserve and eviction of the oldest idle one; when both are busy the call is refused with a teaching error. Expiry is checked on every acquisition. An idle limit is not a lifetime limit, and the docs say so.
4. **An approval is also bound to the session and to the page.** This applies only to pooled sessions; a `host` or other stdio card checks `incarnation` alone.
   - **Session.** The pool keeps a per-`(chat_id, server_id)` epoch that advances every time a session is created and is unique across restarts. At proposal the supervisor writes the epoch into `card_json`, with whether a session was live. At confirm, atomically with acquisition and with registration changes: a different epoch refuses (a session started, or ended and restarted, since the card was shown); a card proposed against a live session refuses if that session has ended; a card proposed with none live, and the same epoch, starts one. A card from before this release has no binding and is refused.
   - **Page.** A live session can change page without changing session. The card shows the page address the session was on when the act was proposed; just before sending an approved act, the pool reads the current address with a read call and refuses if it differs or cannot be read. This relies on the managed Playwright server's own read tools, which is why only managed Playwright is pooled, even if another strict server uses `scope="browse"`.
   - **After an ambiguous timeout the session is ended** before any other exchange is admitted: the timed-out action may still be running in that browser, and matching reply ids does not stop it.
   - Refusals are recorded as **not run**, and each refusal message tells the operator to ask again (`I9`): with a 5-minute idle limit and a slow confirm from a phone, an ended session will be a common case.
5. **Teardown never blocks.** Each child is its own process group. Sessions end on idle expiry, `/stop` in that chat through the one path the web and the channels share (also when no turn is running), `unregister_mcp_server`, a replaced registration, chat delete (hung off the store's chat removal), and serve shutdown. In-flight acquisitions are invalidated by the same events. `reap_idle()` on the ticker skips a busy session without waiting on its exchange, only sends signals, and hands waiting to a daemon thread. A request already sent when teardown arrives is recorded as **outcome unknown**.
6. **Crash orphans: the owner dies with serve.** A saved process-group id cannot be killed safely after a crash: the id can be reused, checking a leader's identity and signalling a group are two separate steps, and a leader can exit while its children live. So nothing is killed from a record. On Linux, bubblewrap's `--unshare-pid --die-with-parent` ends the browser with serve; because that signal follows the **thread** that started the child, the pool starts every child from one long-lived thread of its own. On macOS each pooled child is started under a small owner process that holds a pipe from serve and ends its own process group when the pipe closes. What neither covers is reported, not signalled: `skep doctor` lists a browser record it cannot match to a live session.

### 5. What stays as it is

Policy evaluation (`mcp.<server>.<tool>`, `email/*`, `browse/*`), the distrust of `readOnlyHint`, and the `untrusted` taint on every `call_mcp_tool` result stay in `policy_v3.decide()` and `serve/taint.py`. Forged tools keep their own lane and cannot be set to `host`. Standing policy can still allow a call without a card, as `docs/browser.md` explains. Servers registered before `2.4.0` keep starting unsandboxed, one process per call, inheriting serve's environment as today (operator decision); `skep doctor` flags each one.

## As built in Wave 2a

Where the code lives: `supervisor/mcp_sandbox.py` (validation, the card's
grant, the run-time wall, the install lane, the per-server secrets file,
the bound call, the doctor lines), `supervisor/mcp_cmds.py` (`skep mcp
secret set|list|unset`), the four wall parameters in `wall.walled` and
`sandbox.write_profile`, and wiring in `mcp_client.py`, `serve/tools.py`,
`serve/chat.py`, `serve/cards.py` and `status.py`. Choices the text above
left open:

- **`install` is `(kind, spec)`**, written `npm:<pkg>@<x.y.z>` or
  `pypi:<pkg>==<v>` in the tool call. With `install`, `command[0]` names the
  entry point the package installs (its bin or console script) and the rest
  are its arguments; the card shows the resolved path under
  `~/.skep/mcp/<server_id>/<incarnation>/`. Without it, a strict server's
  `command[0]` must be an absolute path; `npx`, `uvx` and other run-time
  resolvers are refused for `strict`.
- **Every launch checks the registration in force** (review S4): with a
  store, `runner_for_config` checks under the registry gate that the config
  it was given IS the registration now (incarnation included) and holds the
  gate through the exchange, so the startup warm, a discovery or a cache miss
  that read a config before a replacement starts nothing. A cached listing
  is keyed by the exact incarnation. A queued install checks before it
  starts and its discovery handshake runs with the check held; a tree an
  installer is writing is left to it by `retire_trees`, and a stale install
  deletes its own tree. The set of installs in flight is changed and read
  only under the gate, and a successful install leaves it inside its
  publication's own hold (review r2 N5), so a replacement can never land
  between publication and that end and skip a tree no one will remove.
  Forged tools take the same check (review r2 N13): each re-derives what
  runs from its bound record on every launch, and that record, the
  registration and a listing's binding are read under the same read hold,
  so a publication in progress (it holds the write side) is never half seen
  — never the new registration with the old record's hosts. A forged
  exchange's waits are bounded as a stdio one's are.
- **One registration snapshot per call decision** (review S5): the chat
  reads the incarnation once, before the decision classifies the call; the
  in-turn dispatch, the de-duplication and the card all bind to that value,
  passed to each, never read again (review r2 N6: "not read yet" is a value
  of its own, so a server registered before 2.4.0, whose incarnation is
  empty, is a captured value like any other).
  In-turn, the final `decide()` must still allow (an ask is not run, "ask
  again"); on a confirm the card's binding is checked before any policy
  lookup that could list the server.
- **Read roots are resolved at registration** (review S6): the card shows,
  and the registry keeps, where each `read_paths` entry resolves; a confirm
  whose grant no longer matches its card registers nothing, and a launch
  whose recorded root is a link now, or no longer resolves to itself, is
  refused (the idea of `reverify._moved_read_roots`). The check runs again
  once the wall is built (review r2 N7), and on bubblewrap the built wall's
  argv must bind each recorded root exactly at its recorded path, never as
  a link: a root swapped while the wall was built is refused, so the wall
  never grants a directory the card did not show. What remains is the
  instant between that check and bubblewrap's own mount (it mounts by
  path); binding each root from a descriptor opened at the check
  (`--ro-bind-fd`) would close it and is a follow-up.
- **An http registration leaves the sandbox fields out** (review S12): it
  is stored as `host` (the mode that confines nothing); naming any sandbox
  field for it, `sandbox='host'` included, is refused with a teaching error.
- **Pins are exact, and so is the tree's source** (review S9). An npm pin is
  semver 2.0.0 exactly (semver.org's pattern, with node-semver's length and
  size limits): npm reads a malformed prerelease such as `1.2.3-a..b` as a
  mutable tag, which the old pattern let through. A PyPI pin is a canonical
  PEP 440 public version (no local label, no wildcard). The npm lane needs
  npm 11.10.0 or later, read from npm's own `package.json` (nothing runs):
  that release added `--allow-git` (GitHub changelog, 2026-02-18), and the
  install passes `--allow-git=none`, so no git dependency is fetched or
  packed (with its prepare script) anywhere in the tree; an older npm is a
  teaching error. After the install every `package-lock.json` entry must be
  a tarball under the named registry with an integrity hash, or bundled in
  one; a link, file, directory, git or other-host source refuses the whole
  install.
- **The entry point is the pinned package's own** (review S10): npm, a
  `bin` the package's own `package.json` declares, linked to that file;
  Python, a console script its own distribution declares whose launcher its
  `RECORD` lists. It must resolve inside the install tree. Checked at
  publication and again before every launch, so a dependency's bin or the
  venv's own `python` is refused.
- **The install is not staged and renamed** (a Python install's launchers
  hold absolute paths): it installs straight into the incarnation's tree,
  and publication is a marker file written beside the tree, never inside it
  (a wheel can drop files anywhere under its own prefix), after the install
  and the handshake both succeed. Any failure deletes the tree.
- **An unset listed secret refuses the install before anything is
  downloaded**, and the card names the `skep mcp secret set` command for
  each name not yet set. Values may be set before the registration.
- **The secrets file** is `~/.skep/supervisor/mcp-secret/<server_id>-secret`:
  both secret floors already key on `*-secret`, so no floor code changed.
  Every profile build makes the `mcp-secret` dir first (as it makes the relay
  secret), so it is masked while still empty and a sandbox already running
  cannot read the first value set after it started (review S3).
- **A value never reaches the launcher** (review S1). bubblewrap or
  sandbox-exec, the netshim and the Landlock step start with no secret in
  their environment or argv (`--setenv` would put it in a world-readable
  `/proc/<pid>/cmdline`). The last step inside the wall, `skep.secretenv`,
  reads this launch's own snapshot — bound back read-only past its mask
  for that one wall (`walled(unmasked=)`, a fifth wall parameter) — sets the
  listed names and execs the server. A name the wall already set refuses.
- **Each launch gets an immutable snapshot of what it injects** (review r2
  N2): the values of the names the server lists, and only those, read once
  and written to a file of that launch's own (mode 0400, in the masked
  `mcp-secret` dir, outside every root a child can write), deleted when the
  exchange ends. The server's own secrets file is never in its wall, so it
  cannot read a value set for a name it does not list, and a `skep mcp
  secret set` during a launch changes neither what the server gets nor what
  is redacted: redaction masks exactly the snapshot's values, on every path.
- **Secrets for strict servers are Linux-only in this release** (review r2
  N1). A strict server receives its values in its environment. On Linux each
  bubblewrap sandbox has its own PID namespace, so no other sandbox can see
  that process. On macOS, Seatbelt hides the secrets file but does not by
  itself stop another sandboxed process of the same user from reading a
  process's environment (XNU exposes it through `sysctl`), and no Seatbelt
  rule for that has been verified. So on a Seatbelt backend a strict server
  that lists `secret_names` is refused at registration (its card shows the
  error), at launch, and in `skep doctor`, naming the alternatives: no
  secrets, or `sandbox='host'` behind its HIGH card. Follow-up: a verified
  Seatbelt rule (or another delivery) that keeps a value out of every other
  sandbox's reach.
- **One redaction path** (review S2): every call and the install's discovery
  handshake go through `redacted_call`. Each reply line is parsed first and
  the server's data masked after, so a value the JSON escapes is still caught
  in a result or a JSON-RPC error, an `initialize` error included (review r2
  N3: it is masked before it is formatted into the error text); stderr is
  masked a whole line at a time before it is cropped. Every value set is masked, so `skep mcp secret set`
  refuses one shorter than 8 characters, naming the minimum.
- **Outcome unknown is recorded as the card status `interrupted`** (the
  existing status for "it may have run"), with the payload's `outcome`
  saying `unknown`; not run is `failed` with `outcome: not_run`. The card
  statuses did not grow. Every ledger writer carries it (review S14): a card
  confirm, an operator command, and a call standing policy ran in-turn
  (whose row used to be born `confirmed` whatever happened). A removed
  registration or a final deny is a typed not run, never a bad-arguments
  result; a forged call that passes its deadline after its request was fed
  in, or answers nothing, is outcome unknown.
- **A forge rollback restores the previous registration with its
  incarnation** (`restore_mcp_server`): it puts back the registration that
  was in force, so approvals proposed for it stay valid. The whole
  publication — the source, the registration, the plugin record and any
  rollback — runs under one hold of the registry gate (review S13; the gate
  lets its writing thread nest), so no call sees the new incarnation with
  the old binding.
- **No browser path in 2a** (review S7): the run-time wall passes only
  `UV_PYTHON_INSTALL_DIR`; `PLAYWRIGHT_BROWSERS_PATH` is not bound for any
  strict server. Wave 2b must bind a validated browser path only for the
  registration that needs it.
- **serve's port is refused on every local address** (review S8): the
  strict MCP lane's proxy (its run-time and install walls, `walled(
  deny_local=True)`) refuses serve's ports on every address this host owns —
  `0.0.0.0`, an interface address, an IP literal or a resolved answer — not
  only on loopback names. Dispatch, re-verify, forged tools and script
  schedules keep their proxies as they were. serve binds its listener
  before the app starts and records the port it actually listens on (`--port
  0` picks one) before any MCP work can run (review r2 N8), and every wall's
  proxy reads the recorded ports at each admission, not once when it
  started, so a port recorded later is still refused.
- **A valid reply, or outcome unknown** (review S11, r2 N11): only a valid
  `initialize` result advances the handshake — a JSON-RPC 2.0 response whose
  result holds `protocolVersion`, `capabilities` and `serverInfo`, which the
  MCP lifecycle requires (nothing optional is required). After the request
  is sent, a reply that is not one JSON-RPC 2.0 result object or one error
  object (an integer `code` and a `message`) is outcome unknown, and so is a
  `tools/call` result with no `content`, never an empty success. `content`
  is required to be present, not to be an array: skep's own servers, seeds
  and the forge template return it as text. skep's mail and calendar
  servers now send `serverInfo`.
- **One deadline bounds the whole exchange, cleanup included** (review S11,
  r2 N4): every read and every write — the request is written to a raw,
  non-blocking pipe, never through a buffered stream a writer thread holds —
  so a server that floods stdout or stops reading its input is stopped at
  it. The server leads its own process group; at the deadline the whole
  group is killed, so a descendant still holding a pipe end goes with it,
  and every wait in cleanup is bounded: a process that will not die is left
  to a background reaper, so the registry gate a call holds is released
  within the bound.
- **The host disclosure is in the grant line** (review S15): a `host`
  card's risk line itself says the server runs as you and can read skep's
  serve token, so it could answer its own cards — so the disclosure stays
  when another HIGH reason (a doubly tainted chat's) takes the HIGH line.
- **The doctor checks what a launch would run** (review S16): an installed
  strict server's published entry point, a pre-installed one's absolute
  path; only a `host` launcher is looked up on PATH.
- **The host browser keeps its cwd under `~/.skep/browser/`** and inherits
  serve's environment, as before.

## Consequences

- **Good:** an operator-registered stdio server runs behind the same wall as forged tools; a browser can do a multi-step task inside one chat without leaving the sandbox; an approval cannot be re-aimed at a replaced registration or a different session; the audit record tells "not run" from "outcome unknown".
- **Limits, stated plainly (`I8`):**
  - A per-call card gates the call skep makes. It does not constrain what a long-lived server does on its own inside its wall.
  - Open egress confines files and non-public addresses, not what the browser sends to the public web.
  - Only the top package is pinned; its dependencies float at install time. Packages that need install scripts or source builds do not install in the strict lane.
  - A `host` server holds the operator's authority, including skep's serve token; `allowed_hosts` and `read_paths` mean nothing for it.
  - A secret you give a server is that server's to use, including sending it to any host it may reach.
  - Secrets for strict servers are Linux-only in this release: on macOS a strict server that lists `secret_names` is refused (Seatbelt does not by itself keep another same-user sandboxed process from reading a server's environment). A follow-up must verify a rule that does.
  - Tool-name classification (read or act) is a heuristic over names.
  - On Linux, socket scoping by Landlock depends on the kernel; `skep doctor` reports the mode that is enforced, not only the one configured.
  - A platform where the spike fails has no sandboxed browser, and so no multi-step browsing, in `2.4.0`.
  - Other stdio MCP servers stay one process per call.

## Invariant review checklist (`rules/invariants.md`)

1. **Invariants touched:** I3, I5, I6, I8, I9, I11, I12, I13. (I4: nothing here reaches git.)
2. **Can work reach a branch without a human verdict (I1)?** No.
3. **Does any surface trust a worker's self-report (I2)?** No.
4. **Does the Queen gain a side effect, or a worker a voice (I3)?** No. Act verbs still card or follow standing policy; workers get no MCP or browse access.
5. **Does any grant reach the git guards (I4)?** No.
6. **Private permission logic (I5)?** No. Calls decide in `policy_v3.decide()`. The incarnation and generation checks refuse stale approvals; they grant nothing.
7. **Can a timeout, a default or the model confirm anything (I6, I7)?** No. `host` and open egress are HIGH cards. An idle timeout ends a session. The fields that bind an approval are written by the supervisor and cannot be supplied by the model. Nothing is replayed.
8. **Can any surface show a false state (I8)?** The card lists the effective grant before anything runs; `skep doctor` separates configured from enforced; "not run" and "outcome unknown" are distinct records.
9. **Do the errors teach (I9)?** Unpinned spec, a package that needs install scripts, a refused or unset secret name, blocked host, missing backend, session ended, pool full and install failure each name the next step.
10. **Anchored in the record (I10)?** The Phase 03 deferral in `OPERATOR-VERIFICATION.md`, the deferral text in ADR 0068, and the council's file-and-line findings above.

## Council review

Rounds: 4 (the fourth was a confirmation by the one member who was not yet ready)

| Member | Revision 2 | Revision 3 | Revision 4 | Revision 5 |
| --- | --- | --- | --- | --- |
| Claude Opus 5.5 | 5/10 | 6/10 | 8/10, ready | not re-run |
| GPT-6.2 Sol | 5/10 | 6/10 | 7/10, not ready | 9/10, ready |
| GPT-6.1 Terra | 5/10 | 4/10 | 8/10, ready | not re-run |

Revision 5 took all six of Sol's round 3 points and the builder notes from all three. All three members now say the design is ready to build.
