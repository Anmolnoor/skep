# Sandboxing

Skep can run workers under the host OS sandbox backend. The sandbox is defense in
depth: patch approval and re-verification still matter, but sandboxing gives a
physical boundary for network and filesystem writes while the worker runs.

## Backends

| Platform | Backend | Status |
| --- | --- | --- |
| macOS | Seatbelt (`sandbox-exec`) | Supported when the host process may apply profiles |
| Linux | Bubblewrap (`bwrap`) | Supported when `bwrap` is installed and the probe succeeds |
| Linux (opt-in) | podman (`sandbox_backend=podman`) | Runs with SELinux labelling disabled for the container; the probe proves a mount round trip |
| Other | none | Unavailable |

Skep probes the backend before launch. If sandboxing is requested but no backend
can be applied, worker launch fails instead of silently running unsandboxed.

## Filesystem Boundary

The sandbox mounts the root filesystem read-only and binds only the intended
writable roots back as writable:

- the worker workspace (its private `TMPDIR` lives inside it)
- the run's own result directory (`results/<task_id>/`, where `--out` points);
  no run can write another run's result, and the supervisor rejects (and
  records on the run) an envelope whose `task_id` is not the run's
- the worktree's own git metadata and the repo's `objects/` — enough for
  `git add`/`git commit`; the repo's `hooks/`, `config` and `refs/` stay
  read-only, and skep's own host-side git runs with hooks and fsmonitor off.
  On macOS a worker's `git commit` therefore prints `error: Unable to create
  '<repo>/.git/packed-refs.lock': Operation not permitted` and exits 0: the
  commit succeeded, and the message is expected.
- any explicit extra writable roots from supervisor configuration

Shared temp dirs (`/tmp`, `/var/folders`) are not writable, and a run whose
skep home lies inside a writable root is refused.

This blocks writes outside the workspace/results boundary. Reads are open so
the toolchain works, except the secret floor, which is hidden on every
backend: `~/.ssh ~/.aws ~/.gnupg ~/.config/gh ~/.netrc ~/.git-credentials
~/.docker/config.json ~/.kube ~/.secrets ~/.config/gcloud ~/.azure ~/.pypirc
~/.vault-token ~/.terraform.d/credentials.tfrc.json ~/.config/op
~/.cargo/credentials{,.toml} ~/.claude.json ~/.password-store`, the shell rc
files (`~/.bashrc ~/.bash_profile ~/.profile ~/.zshrc ~/.zshenv ~/.zprofile`)
and REPL histories (`~/.bash_history ~/.zsh_history ~/.python_history
~/.node_repl_history ~/.psql_history ~/.mysql_history ~/.sqlite_history`),
and the skep home's `serve.env`, `supervisor/serve-token`,
`supervisor/*-secret`, `supervisor/llm-secret*`, `supervisor/audit/` (every
run's audit trail) and `supervisor/chat-attachments/`. One carve-out: a
worker whose caste calls the LLM (coding, document, reviewer) reads
`llm-secret` and the active provider profile's `llm-secret-<id>`, its own
model credential; external engines and every other sandboxed lane see no LLM
key. With the provider-key relay on (`provider_key_relay`, ADR 0062; off by
default), a builtin worker gets no carve-out: its model calls go through a
token-checked route on its run's filtering proxy, which adds the key, so the
key files stay masked and no variable carrying the key reaches its env. Not
relayed: bedrock (SigV4 signs with the key), a provider with no key, and the
external engines, which keep their own auth env (`skep doctor` says which). `supervisor/supervisor.sqlite3` stays readable (that worker reads its
provider settings from it). So does `~/.npmrc` (npm install): the sandbox
leaves it readable, while the policy floor still refuses every Queen and
worker read tool on it (`sandbox.SANDBOX_READABLE_SECRETS`). The engine
logins — `~/Library/Keychains`, `~/.claude/.credentials.json`,
`~/.codex/auth.json` — are masked like every other credential (W33): an
external engine's config dir is redirected per run and it authenticates only
from its auth env (`CLAUDE_CODE_OAUTH_TOKEN`/`ANTHROPIC_API_KEY`,
`OPENAI_API_KEY`); a dispatch without one is refused (NO AUTH ENV). One
exception, opt-in per project: a codex run with no `OPENAI_API_KEY` may use
the operator's ChatGPT plan login through an access-token-only, read-only
copy that only that run's sandbox can read (it sits in the masked audit dir
and is bound back for its own run) and that is deleted when it ends — the
real `~/.codex/auth.json` stays masked and the refresh token never enters the
sandbox ([`codex-chatgpt-login.md`](codex-chatgpt-login.md)).

On Linux the worker also gets its own PID and IPC namespaces and an empty
`/run` (no host D-Bus or systemd sockets).

Unix sockets on Linux: a read-only bind does not stop `connect()`, so
bubblewrap closes a socket only by hiding it. Hidden in every network mode:
everything under `/tmp` and `/run` (fresh tmpfs: the session D-Bus,
systemd, rootless Docker and podman, gpg-agent's runtime dir, most
ssh-agents); the agent places under every home (`~/.ssh`, `~/.gnupg` and
`sandbox.AGENT_SOCKETS`: `~/.1password`, `~/.bitwarden-ssh-agent.sock`,
`~/.docker/desktop`, `~/.docker/run`, `~/.colima`, `~/.lima`, `~/.rd/docker.sock`,
`~/.orbstack/run`, `~/.local/share/containers/podman/machine`); the socket
serve's own `$SSH_AUTH_SOCK`, or a `unix://` `DOCKER_HOST` or
`CONTAINER_HOST`, names; and every socket inside a read-only root the sandbox
binds (the repo's git dir: git's fsmonitor socket). A worker still uses
sockets in its own roots, and a proxied run its proxy bridge.

Still reachable on Linux, by the kernel's Landlock ABI (see Landlock below),
and stated by `skep doctor` in its sandbox line:

- ABI 9 and later: no socket the worker did not make, except those inside
  its own writable roots. Neither the masks nor Landlock touch a descriptor
  the worker already holds when it starts.
- ABI 6 to 8: a unix socket at any other path outside `/tmp` and `/run` (an
  app socket somewhere under a home), in every network mode; on podman, any
  such path under the host dirs it binds (`/home /var /etc /usr /opt`).
- Below ABI 6, or Landlock off: that path socket, and an abstract socket (a
  name starting with a NUL byte: the session D-Bus where it listens
  abstractly, X11's `@/tmp/.X11-unix/X0`) in a bubblewrap allow-all run.
  Abstract sockets belong to the network namespace, so deny-all and proxied
  runs (`--unshare-net`) cannot reach them, nor can any podman run (it
  always gets its own network namespace).

Bubblewrap has no socket filter of its own. A seccomp filter (`--seccomp`)
cannot do it either: classic BPF sees only the `connect()` arguments, not
the address the pointer leads to, so it cannot tell an abstract name from a
path.

### Landlock (Linux)

Landlock, the kernel's unprivileged access control, closes what the masks
cannot. Where it is applied: inside the sandbox, by `skep.landlock`, a
standard-library step (ctypes and `syscall()`, no dependency) that bubblewrap
or podman runs first; it sets `PR_SET_NO_NEW_PRIVS`, restricts itself and
then execs the worker, which inherits the restriction and cannot lift it.
In a proxied run it runs after `skep.netshim` (which needs the proxy
bridge) and before the worker. It never runs in serve: serve only reads the
kernel's Landlock ABI number when it writes the run's profile and puts the
step, with what it must scope, into the argv (kept as evidence).

What is scoped, by the ABI the running kernel reports:

- ABI 6 and later (Linux 6.12+): `LANDLOCK_SCOPE_ABSTRACT_UNIX_SOCKET`. The
  worker cannot connect to an abstract socket made outside its own process
  tree; ones it makes itself still work.
- ABI 9 and later: `LANDLOCK_ACCESS_FS_RESOLVE_UNIX`. The worker connects to
  a path socket only inside its writable roots (workspace, results, git
  metadata, caches) or one it made itself; any other path socket, listed or
  not, is refused with `EACCES`.
- With `RESOLVE_UNIX` the step also handles `LANDLOCK_ACCESS_FS_REFER` and
  grants it on `/`: the kernel otherwise refuses every cross-directory
  rename and hard link once any filesystem right is handled. Its no-gain
  rule still refuses moving or linking a socket into a writable root. No
  other right is handled: file access stays the mount layout's job.

On an older kernel, or with Landlock disabled at boot, serve leaves the step
out, `skep doctor` says so in one line, and runs carry on exactly as before
with the gaps below. If the kernel reports the ABI but applying it fails,
the run fails: it never runs unscoped while the doctor says scoped.

Podman runs the same step inside the container (its default seccomp profile
allows the Landlock calls). Abstract sockets cannot reach a podman run
anyway (its own network namespace), so there it is the path-socket right
that matters.

Landlock only ever narrows. Every bubblewrap and podman mask above stays:
they also hide what a socket file reveals by being read, and they are the
wall on a kernel without Landlock.

### Loopback

Loopback is closed to a sandboxed worker by default. A run reaches a port on
this host only when it is:

- its own model provider's port, when that provider is local (for example
  Ollama on `127.0.0.1:11434`), or
- a port the project policy lists in `loopback_ports`.

Serve's port is never reachable, even when a project lists it: 8765,
`$SKEP_SERVE_URL`'s port, and the port `skep serve` records in
`supervisor/serve-port` under the same home, so a CLI `skep run` refuses it
too. The dispatch card and manifest show the loopback ports a run gets.

To open a port for a project's workers (a local database or test server):

```sh
skep project setup /path/to/repo --loopback-port 5432
```

From chat, `setup_project` with `policy_overrides: {"loopback_ports": [5432]}`
(it shows a card). Ports run 1024-65535: a port below 1024 is privileged inside
bubblewrap's network namespace, netshim could not present it and the worker
would not start, so setup refuses it. HTTP to a closed port answers `403` naming the port and that
command; a raw connection on macOS fails with "Operation not permitted".
Re-verification gets the same project ports. `skep doctor` and
`skep policy migrate --dry-run` name the projects that open none.

How each backend enforces it:

- **macOS (Seatbelt):** the profile refuses every connection to `localhost`
  except the run's ports and the filtering proxy's port. Binding and
  accepting stay open (they reach nothing). Loopback HTTP goes through the
  proxy, so a closed port gets the explanatory `403`.
- **Linux (bubblewrap):** a deny-all or domain-list run has its own network
  namespace, so its loopback is not the host's. The host's loopback is
  reachable only through the proxy bridge, which admits the run's ports
  alone, and `skep.netshim` presents each of them on the sandbox loopback
  (raw TCP too). A run with no proxy (no provider, no hosts) reaches no host
  port. A `*` run shares the host network namespace: bubblewrap has no port
  filter there, so its loopback is the host's, serve's port included.

A Seatbelt worker connects only to unix sockets under its own writable roots
(W34): the operator's ssh-agent, Docker, gpg-agent and other local daemons are
refused in every network mode. An allow-all run may also reach
`/var/run/mDNSResponder` (name lookups); a deny-all or proxied run may not, so
a name cannot carry data out through the system resolver (the proxy resolves
for it; `localhost` resolves from `/etc/hosts`).

## Network Modes

Skep models network access as a domain list:

- empty list: deny all outbound network
- `*`: allow all outbound network
- concrete domains: allow only those domains, when the backend can enforce it

A dispatch that names hosts adds them to the project's own (`default_network`,
the trusted-dev registries and remembered hosts); an explicit empty list leaves
the worker only its own model provider.

Whatever the mode, every worker env carries `GIT_ALLOW_PROTOCOL=file`: git
inside a worker (and every process it starts) uses only its local transport,
so a push, fetch, clone or submodule update over https, ssh or git:// fails
with "transport not allowed". That includes a package manager's git-sourced
dependencies (`git+https://…` in uv/pip, `github:` in npm) unless they are
already in the cache.

A process that clears its env (`env -i git push`, `subprocess.run(..., env={})`)
drops the variable, so skep's task worktrees carry the same rule as git
config, which git reads with an empty environment. The first time skep runs a
task in a repository it writes `.git/skep-protocol-pin` (`protocol.allow=never`,
`protocol.file.allow=always`) and adds one section to that repository's
`.git/config`:

```ini
[includeIf "gitdir:./worktrees/????????-????-7???-????-????????????*"]
	path = skep-protocol-pin
```

It matches only a linked worktree whose git dir is named like a skep task id
(a UUIDv7): your checkout and your own worktrees read none of it, and it is
plain config every git client parses (no repository extension). To remove it:
`git config --remove-section 'includeIf.gitdir:./worktrees/????????-????-7???-????-????????????*'`
and delete the file; skep writes both again on its next run there. In sandbox
mode both files are read-only to the worker (it cannot edit, delete or
replace them). In workspace mode there is no sandbox: the worker can edit or
delete them, so there the rule only stops a script that clears its env and
does nothing else.
Neither carrier stops git told otherwise on its command line
(`git -c protocol.allow=always`), a `GIT_CONFIG_*` variable, a `.git` file
repointed at another git dir, or a fresh clone in the workspace: the network
mode above is the wall. skep's own pushes (open_pr, push_branch) run in your
checkout, which reads none of this.

### macOS

For concrete domain allowlists, Seatbelt pins worker egress to Skep's loopback
filtering proxy. Seatbelt enforces that the worker can only talk to the proxy;
the proxy enforces the allowed domains.

### Linux

Bubblewrap enforces:

- deny-all network with `--unshare-net`
- allow-all network by omitting `--unshare-net`
- concrete per-domain allowlists (v28): the netns keeps `--unshare-net` (no
  route out), the private host directory holding the filtering proxy's AF_UNIX
  socket is bind-mounted in read-only, and `skep.netshim` presents the proxy
  port on the sandbox loopback and bridges it to that socket. Egress is exactly
  as tight as deny-all, plus one filtered pipe — the same guarantee Seatbelt
  gives on macOS.
- `--die-with-parent` in every invocation. With serve's restart re-adoption on
  (the default, ADR 0060), bwrap's parent is a small host keeper process
  rather than serve, so a sandbox outlives a serve restart and is re-adopted;
  the keeper kills it a minute past its wall-clock budget if no serve comes
  back. `skep serve --no-readopt` makes bwrap serve's child again.

## Running With The Sandbox

Use sandbox mode explicitly:

```sh
skep run /path/to/repo "fix the failing test" --execution-mode sandbox
```

On Linux, install bubblewrap first:

```sh
# Fedora
sudo dnf install bubblewrap

# Ubuntu/Debian
sudo apt install bubblewrap
```

## Troubleshooting

If `skep run --execution-mode sandbox` fails before the worker starts, check the
sandbox probe:

```sh
skep doctor
```

Common causes:

- `bwrap` is missing on Linux.
- Bubblewrap is installed but user namespaces are disabled by host policy.
- `sandbox-exec` exists on macOS but the parent process is not allowed to apply
  Seatbelt profiles.

When you need a live provider or package registry during early setup, use an
explicitly broader policy or `--execution-mode workspace` for that trusted run.
