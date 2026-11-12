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
- the results/audit output directory
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
key. `supervisor/supervisor.sqlite3` stays readable (that worker reads its
provider settings from it). So do `~/.npmrc` (npm install), `~/.claude/.credentials.json`,
`~/.codex/auth.json` and `~/Library/Keychains` (engine logins): the sandbox
leaves them readable, while the policy floor still refuses every Queen and
worker read tool on them (`sandbox.SANDBOX_READABLE_SECRETS`).

On Linux the worker also gets its own PID and IPC namespaces and an empty
`/run` (no host D-Bus or systemd sockets). On macOS the profile allows
loopback (a worker's own test servers) except serve's port: 8765,
`$SKEP_SERVE_URL`'s port, and the port `skep serve` records in
`supervisor/serve-port` under the same home, so a CLI `skep run` denies it too.

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
already in the cache. A process that rewrites its own env can drop the variable; the
network mode above is the wall.

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
