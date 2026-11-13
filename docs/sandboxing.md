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
`OPENAI_API_KEY`); a dispatch without one is refused (NO AUTH ENV).

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

Still reachable on Linux, and stated by `skep doctor`:

- a unix socket at any other path outside `/tmp` and `/run` (an app socket
  somewhere under a home), in every network mode; on podman, any such path
  under the host dirs it binds (`/home /var /etc /usr /opt`);
- an abstract socket (a name starting with a NUL byte: the session D-Bus
  where it listens abstractly, X11's `@/tmp/.X11-unix/X0`) in a bubblewrap
  allow-all run. Abstract sockets belong to the network namespace, so
  deny-all and proxied runs (`--unshare-net`) cannot reach them, nor can any
  podman run (it always gets its own network namespace).

Bubblewrap has no socket filter of its own. A seccomp filter (`--seccomp`)
cannot do it either: classic BPF sees only the `connect()` arguments, not
the address the pointer leads to, so it cannot tell an abstract name from a
path. What would close both is Landlock, the kernel's unprivileged access
control, applied by a small step inside the sandbox before the worker
starts: `LANDLOCK_SCOPE_ABSTRACT_UNIX_SOCKET` (Landlock ABI 6, Linux 6.12)
refuses abstract sockets made outside the worker, and the newer
path-socket right refuses a connect to a path socket outside the rules
(the worker's own roots and the proxy bridge). Both can be called from the
standard library (no new dependency); older kernels would keep the gap
above. Not built yet.

On macOS the profile allows
loopback (a worker's own test servers) except serve's port: 8765,
`$SKEP_SERVE_URL`'s port, and the port `skep serve` records in
`supervisor/serve-port` under the same home, so a CLI `skep run` denies it too.
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
  route out), the host filtering proxy's AF_UNIX socket is bind-mounted in, and
  `skep.netshim` presents the proxy port on the sandbox loopback and bridges it
  to that socket. Egress is exactly as tight as deny-all, plus one filtered
  pipe — the same guarantee Seatbelt gives on macOS.

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
