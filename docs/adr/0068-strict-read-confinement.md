# ADR 0068 — strict read confinement: a sandboxed run reads an allowlist

**Status:** accepted (Phase 03, Work Item 3.2). Linux bubblewrap, the G10
re-verify and the Queen's file reads are done; the macOS Seatbelt half is
separate work that builds on the seam below.
**Related:** ADR 0066 (new projects sandboxed by default; this ADR copies its
rollout rule), ADR 0047 (external engines: the sandbox is their only wall),
ADR 0062 (provider-key relay). Invariants touched: I4, I5, I8, I9, I12.

## Context

The sandbox walls writes and network, but reads were a denylist.
Bubblewrap's `--ro-bind / /` and Seatbelt's `(allow default)` hid only the
secret floor (`sandbox.secret_paths`). Everything else on the disk stayed
readable to a sandboxed worker: `~/Downloads`, `~/Documents`, other projects'
checkouts, `/mnt`, `/media`, `/srv`, `/var` (including `/var/lib/vz` and an
alternate `/var/home`), `/etc/pve`, sibling runs' worktrees and results, and
the operator's own checkout of the repo. That is the "Hermes roamed
`~/Downloads`" failure, and the one that made a Proxmox host's notes
readable. A denylist cannot keep up with every place a person keeps files.

## Decision

1. **A per-project knob, `strict_read_confinement`.** On a strict project,
   bubblewrap starts from its own empty root, not the host's. Read-only, in
   this order:
   - the system dirs `/usr`, `/opt` and `/nix` when present; the usr-merge
     links `/bin`, `/sbin`, `/lib`, `/lib64` (`/lib32`, `/libx32`) are
     recreated as links, or bound where they are real dirs;
   - `/etc`, entry by entry, without `pve`, `shadow`, `shadow-`, `gshadow`,
     `gshadow-`, `sudoers`, `sudoers.d` and `ssh`. Those entries do not
     exist inside the sandbox. Nothing is mounted over them, so a FUSE
     `/etc/pve` the user cannot stat never stops a run;
   - `--dev /dev`, `--proc /proc` (the run's own pid namespace),
     `--tmpfs /tmp`, `--tmpfs /run`, plus the one `resolv.conf` rebind;
   - `sandbox.strict_read_roots()`, each resolved and existing:
     - skep's own interpreter: `sys.prefix`, `sys.base_prefix`,
       `sys.exec_prefix`, skep's package root, and every `site-packages` /
       `dist-packages` entry on `sys.path`. That last one covers uv's
       `run --with` overlay env and an editable install, whose `.pth` chain
       leads elsewhere.
     - the per-user toolchains: `~/.local/bin`, `~/.local/share/uv/python`
       (`$UV_PYTHON_INSTALL_DIR`), `~/.local/share/uv/tools`
       (`$UV_TOOL_DIR`), `~/.local/share/claude`, `~/.cargo`
       (`$CARGO_HOME`), `~/.rustup` (`$RUSTUP_HOME`), `~/.nvm` (`$NVM_DIR`),
       `~/.pyenv` (`$PYENV_ROOT`).
     - `<skep home>/tools/.venvs`.
     - from the skep home, the files the builtin worker reads:
       `profile.json` and `worker_plugins/`. The store and the worker's own
       LLM key are added only for an LLM-calling worker that holds its key
       (no relay).
     - the operator's `extra_read_paths`.
     - the worker command's own files: each absolute, existing, non-link file
       in the operator's `--worker-cmd` / `SKEP_WORKER_CMD` argv (its script),
       never its directory, so the script runs and its siblings stay out.

     Every root is judged where it resolves, before anything walks or binds
     it. A detected root at or above `$HOME`, or on `/etc` or its hidden
     entries, is dropped; a per-user toolchain that is itself a symlink (a
     `~/.local/bin` linked to `~/Downloads`) is dropped too, with a warning in
     the serve log, and the operator follows such a link on purpose by naming
     it in `extra_read_paths`. An extra path at or above `$HOME` (so `/`,
     `/home`, `/var/home`), in or over the skep home, or on `/etc` or its
     hidden entries refuses the run (`sandbox.extra_read_path_problem`); setup
     refuses the same paths first, naming a subdirectory to use. A written
     root that is a symlink (uv's `cpython-3.12-…` minor-version link, which a
     venv's python points through) is recreated as a link to its resolved
     root.
   - the run's git metadata: the common dir's entries, without `worktrees/`
     (the sibling runs' HEAD and index), `objects/` (already writable), any
     symlinked entry (bound by its own name, its target would be readable),
     and the main checkout's own state a linked worktree's git never reads:
     `index`, `COMMIT_EDITMSG`, `MERGE_MSG`, `ORIG_HEAD`, `refs/stash` and
     `logs/refs/stash`. `git status`, `git diff` and `git log` still run.
   - the run's writable roots, as before: worktree, results dir, run-private
     caches.
   - the secret masks, **last**, so no `extra_read_paths` entry or detected
     root can expose `~/.ssh`, `serve-token` or the store.
   - finally `--remount-ro /`, so the empty root's own directories take no
     writes. A write outside the writable roots still fails with EROFS, as
     it does under the host root.
2. **G10 runs inside the same wall.** Dispatch resolves the wall once, from
   the project the run already resolved (its `project_context` binding
   first, so a project bound by template name alone counts), and records it
   on the run's `dispatched` line as `read_allowlist` (the extra paths; the
   key is absent for a denylist-only run). `reverify_run`, live or after a
   restart, reads that record and never the policy again, so a policy edit
   between the worker and its re-verify cannot widen G10's reads. The
   baseline prime, the patched prime and the verify command all get the
   strict profile. A patch is never re-verified with wider reads than its
   worker had.
3. **Fail closed.** `write_profile(read_allowlist=…)` on a backend outside
   `STRICT_READ_BACKENDS` raises `SandboxUnavailableError`, naming the fix,
   rather than running with open reads. Today that is Seatbelt. A host
   configured with `sandbox_backend: podman` runs a strict run on native
   bubblewrap instead, as it already does for a domain-list run
   (`resolve_sandbox_backend`, loudly in the log); only an explicit podman
   profile write refuses. Dispatch records `sandbox_unavailable`; G10
   records `unavailable`.
4. **Rollout, as ADR 0066 did for the mode.** A NEW project whose mode
   resolves to `sandbox` gets `strict_read_confinement: true` at setup and
   in the preview, but only where the host backend enforces it
   (`projects.default_strict_reads`). A Mac therefore never gets a flag
   that would refuse every run; a Linux host configured for podman does get
   it, and its strict runs take native bubblewrap (decision 3). An explicit value wins. An
   existing project keeps what it stored through re-setup and phase changes.
   `skep doctor` names the sandboxed projects still on denylist-only reads,
   with the command that moves one:
   `skep project setup <repo> --project-id <id> --strict-reads`. Chat and
   REST set it through `policy_overrides` (`strict_read_confinement`,
   `extra_read_paths`), which go through the same validator: paths must be
   absolute, never `/`.
5. **The doctor says which wall is up, and where.** On bubblewrap the
   sandbox line reads `read confinement: strict-allowlist available; N of M
   sandboxed projects strict; forged tools, MCP servers, the fast lane and
   diagnose re-runs stay denylist-only` (`strict_projects` and
   `sandboxed_projects` in the JSON); on Seatbelt it reads
   `read confinement: denylist-only (reads stay open but the secret floor)`.
6. **The Queen.** Her file reads were already scoped to `operator_roots()`,
   and an absolute path outside them cards. What was left was a bare name
   ("read `canary-downloads.txt`"): it resolved against serve's working
   directory, which is often `$HOME`. It is now refused before any
   resolution (`filesystem.deny.outside_root_lookup`,
   `default/root-boundary`). The refusal names the roots she reads, says she
   never searches the home or `~/Downloads` for a name, and asks for the
   absolute path (I9).

## What a strict run still reads (I8, stated plainly)

- Every world-readable file under `/usr`, `/opt`, `/nix` and the kept `/etc`
  entries: `passwd`, `group`, `hosts`, `machine-id`, `os-release`, and so on.
  `/etc` files that hold secrets are root-only on a normal host, so the
  worker, which is not root, cannot open them.
- All of each detected toolchain dir. `~/.local/bin` holds the operator's own
  user scripts too. The cargo credentials (`credentials`, `credentials.toml`)
  stay masked under whichever root is bound, `~/.cargo` or a moved
  `$CARGO_HOME`. Still readable: cargo's `config.toml` (a
  `[registries] token` there is readable, as cargo itself needs the file)
  and nvm's per-version global npmrc (`$NVM_DIR/versions/node/*/etc/npmrc`),
  which npm reads the way it reads `~/.npmrc`
  (`sandbox.SANDBOX_READABLE_SECRETS`).
- The repo's shared git metadata: `config`, which git cannot run without and
  which can carry credentials (an `http.extraheader`, a token in a remote
  URL); refs but the stash, `packed-refs`, branch reflogs, `hooks/`, `info/`.
  `objects/` is shared too: every object in it is readable by hash,
  including blobs only a sibling run, the operator's staged index or a
  stash ever referenced.
- With a dev checkout's serve, skep's own interpreter and package roots are
  that checkout's `.venv` and `src/`, so a strict run reads that much of the
  operator's checkout.
- Its own worktree, results dir and caches, read and write.
- The **names** of masked credential paths that exist, never their contents.
  The masks are laid down even where nothing is bound, so `~/.bashrc` shows
  up as an unreadable placeholder and `~/.ssh` as an empty dir.

Not readable: the rest of `$HOME`, other users' homes, `/var`, `/mnt`,
`/media`, `/srv`, `/root`, `/boot`, `/etc/pve`, sibling worktrees and their
git metadata, other runs' results and audit trails, other managed repos, the
operator's own checkout of the repo, the skep home's other files, and host
`/tmp` and `/run`.

## The Seatbelt seam (the macOS half)

`strict_read_roots(skep_home, *, worker_llm_key, extra)` is the
host-independent half. Every backend's profile allows exactly these, plus its
OS's system roots and the run's writable and git roots. The Seatbelt builder:

- consumes it in `build_profile` in strict mode, with a global
  `(deny file-read*)`;
- re-allows macOS's system roots (`/usr`, `/System`, `/Library`,
  `/opt/homebrew`, `/bin`, `/sbin`, `/dev`, a safe `/private/etc` subset),
  plus the writable roots, git roots, run `TMPDIR` and these roots;
- adds `file-read-metadata` on the ancestor chain;
- emits the secret denies last;
- then adds `"seatbelt"` to `STRICT_READ_BACKENDS`, which removes its
  refusal in `write_profile` and turns on the new-project default on Macs.

## Not covered yet

- Forged tools (`mcp_client.py`), the fast lane (`serve/fastlane.py`), the
  `diagnose` re-run (`serve/actions.py`) and MCP servers still get
  denylist-only profiles. None of them is a project run with a policy to
  read; each needs its own call on what it must read.
- The worker's prompt teaches the write and network walls, not the read
  wall. A read outside the wall fails as "No such file or directory", which
  the worker sees as a plain missing file (an I12 follow-up).
- A toolchain outside the detected list needs `extra_read_paths`. The
  detected toolchains are not walked for unix sockets (`~/.rustup` alone is
  tens of thousands of files); the operator's extra paths are.
- A root reached through a linked parent dir keeps only its resolved name.
  On Fedora Atomic, for example, `$HOME` is normally `/var/home/<user>`
  already, so this rarely matters.
- Hosts that keep system software under `/var` or `/run` lose it in a
  strict run, and fail closed rather than read more: on Fedora Atomic
  `/opt` and `/usr/local` are links into `/var`, which is not bound, and on
  NixOS the profile under `/run/current-system` is gone with host `/run`.
  Such a host needs `extra_read_paths` (e.g. `/var/opt`) or denylist-only
  reads.

## Consequences

- New sandboxed projects on Linux read only what a build needs. Python, git,
  `uv run pytest` and node are proven inside both the worker wall and the
  G10 wall by `tests/supervisor/test_sandbox_read_confinement.py`, against
  real bubblewrap.
- Existing projects do not change until the operator runs the doctor's
  command. The live `~/.skep` is untouched by this change.
- On Macs nothing changes until the Seatbelt half lands. A project explicitly
  set strict there is refused, and never silently run with open reads.
