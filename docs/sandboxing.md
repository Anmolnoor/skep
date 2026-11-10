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
  read-only, and skep's own host-side git runs with hooks and fsmonitor off
- any explicit extra writable roots from supervisor configuration

Shared temp dirs (`/tmp`, `/var/folders`) are not writable, and a run whose
skep home lies inside a writable root is refused.

This blocks writes outside the workspace/results boundary. Reads are open so
the toolchain works, except the secret floor, which is hidden on every
backend: `~/.ssh ~/.aws ~/.gnupg ~/.config/gh ~/.netrc ~/.git-credentials
~/.docker/config.json ~/.kube` and the skep home's `serve.env`,
`supervisor/serve-token` and `supervisor/*-secret`. `llm-secret*` stays
readable: the builtin worker authenticates to its model with it.

On Linux the worker also gets its own PID and IPC namespaces and an empty
`/run` (no host D-Bus or systemd sockets). On macOS the profile allows
loopback (a worker's own test servers) except serve's port.

## Network Modes

Skep models network access as a domain list:

- empty list: deny all outbound network
- `*`: allow all outbound network
- concrete domains: allow only those domains, when the backend can enforce it

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
