# ADR 0068 — strict read confinement: a sandboxed run reads an allowlist

**Status:** accepted (Phase 03, Work Item 3.2). Linux bubblewrap, the G10
re-verify and the Queen's file reads are done. The macOS Seatbelt half was
added on 2026-11-23 (see "The Seatbelt half").
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
       (no relay). Each of these skep-home roots, `tools/.venvs` included, is
       bound only where it sits in the home: one reached through a link of
       its own or a linked parent (a `worker_plugins` linked to `~/Downloads`)
       is dropped, with a warning in the serve log. A relocated plugin shelf
       is named in `extra_read_paths`, which are judged.
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
   on the run's `dispatched` line as `read_allowlist`: the extra paths'
   resolved targets, each judged by `extra_read_path_problem` first (a link
   that now points at the home refuses the run as `sandbox_unavailable`).
   The key is absent for a denylist-only run. A worker that had no wall
   (workspace mode, or the sandbox off) records the same list as
   `g10_read_allowlist` instead, so its record never claims a wall its
   worker did not have (I8); G10 still re-runs that patch inside the
   project's wall, narrower than what the worker read. `reverify_run`, live
   or after a restart, reads that record and never the policy again, and
   binds exactly those targets: one that is now a link, or no longer
   resolves to itself (a parent swapped for a link), makes G10
   `unavailable`, naming the path and where it leads, and nothing runs. So
   neither a policy edit nor a retargeted link between the worker and its
   re-verify widens G10's reads. The baseline prime, the patched prime and
   the verify command all get the strict profile. A patch is never
   re-verified with wider reads than its worker had.
3. **Fail closed.** `write_profile(read_allowlist=…)` on a backend outside
   `STRICT_READ_BACKENDS` raises `SandboxUnavailableError`, naming the fix,
   rather than running with open reads. Today that is only an explicit
   podman profile write: Seatbelt joined `STRICT_READ_BACKENDS` on
   2026-11-23, and a host configured with `sandbox_backend: podman` runs a
   strict run on native bubblewrap instead, as it already does for a
   domain-list run (`resolve_sandbox_backend`, loudly in the log).
   Dispatch records `sandbox_unavailable`; G10 records `unavailable`.
4. **Rollout, as ADR 0066 did for the mode.** A NEW project whose mode
   resolves to `sandbox` gets `strict_read_confinement: true` at setup and
   in the preview, but only where the host backend enforces it
   (`projects.default_strict_reads`): bubblewrap and, since 2026-11-23,
   Seatbelt. A Linux host configured for podman gets it too, and its strict
   runs take native bubblewrap (decision 3). An explicit value wins. An
   existing project keeps what it stored through re-setup and phase changes.
   `skep doctor` names the sandboxed projects still on denylist-only reads,
   with the command that moves one:
   `skep project setup <repo> --project-id <id> --strict-reads`. Chat and
   REST set it through `policy_overrides` (`strict_read_confinement`,
   `extra_read_paths`), which go through the same validator: paths must be
   absolute, never `/`.
5. **The doctor says which wall is up, and where.** On bubblewrap and
   Seatbelt the sandbox line reads `read confinement: strict-allowlist
   available; N of M sandboxed projects strict; forged tools, MCP servers,
   the fast lane and diagnose re-runs stay denylist-only`
   (`strict_projects` and `sandboxed_projects` in the JSON); on a backend
   that cannot enforce it, `read confinement: denylist-only (reads stay
   open but the secret floor)`.
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

## The Seatbelt half (2026-11-23)

`strict_read_roots(skep_home, *, worker_llm_key, extra)` is the
host-independent half: every backend allows exactly these, plus its OS's
system roots and the run's writable and git roots. On a strict project,
`build_profile` adds these rules after the write rules (SBPL: the last
matching rule wins):

```
(allow default)
(deny lsopen) (deny job-creation) (deny appleevent-send) (deny mach-lookup <mds>)
...network and write rules, unchanged...
(deny file-read* file-test-existence (with errno ENOENT))
(allow file-read* file-test-existence (literal "/"))
(allow file-read-metadata file-test-existence <every ancestor, literal>)
(allow file-read-metadata file-test-existence (subpath "/etc") (subpath "/var/select") ...)
(allow file-read* file-test-existence (subpath "/usr") (subpath "/System") ... <the run's roots>)
(allow file-read* file-test-existence (literal <the repo's shared refs/>))
(deny file-read* file-test-existence (subpath "/System/Volumes") (with errno ENOENT))
(deny file-read* <the secret floor>) ... (deny file-read* (literal "/private/etc/sudoers")) ...
(deny file-read* (subpath "/opt/homebrew/var")) ...
(allow file-read* (literal <this run's own unmasked file>))
```

- **Nothing starts outside the wall (review round 2, M9).** Every Seatbelt
  profile, strict or not, denies `lsopen`: on macOS 27, `open` handed
  LaunchServices an app (or a `.command`) that ran unsandboxed and copied a
  fake-HOME canary into the worktree, under both profiles. `appleevent-send`
  closes the same door through another app (`tell application "Terminal"
  to do script`), `job-creation` a launchd job (launchd already refuses a
  sandboxed `launchctl submit`, `bootstrap` and `load` on macOS 27; the
  deny does not rely on that), and `com.apple.metadata.mds` Spotlight, an
  index of the whole disk (macOS 27 filters its answers by the caller's
  reads; the deny does not rely on that either). A lookup deny on
  `com.apple.coreservices.launchservicesd` was tried and dropped: it did
  not stop `open`, and LaunchServices also answers ordinary bundle lookups.
- **`/System/Volumes` closed again (M4).** `/System` is reopened whole, and
  that reached `/System/Volumes`: `Preboot`, `VM`, `Update` and the data
  volume's own entries no firmlink maps back to the root (`mnt`, `sw`,
  `.PreviousSystemInformation`). A firmlinked path is matched by its root
  spelling, so `/Users/...` is never under it; dyld, Python, git, uv and
  node still run.
- **The shared `refs/` is listed (M3).** The git roots are `refs/`'s
  entries, never the dir, so Seatbelt left it metadata-only and `git
  for-each-ref` and `git branch` found nothing and exited 0. It is listed
  now (names only); `refs/stash` stays unreadable, so git calls it a broken
  ref: `for-each-ref` warns, and `git log --all` and `git prune` stop
  (`bad object refs/stash`, `unable to parse object: refs/stash`) while the
  operator's checkout has a stash. Before, `git prune --dry-run` in the wall
  listed that stash's objects for deletion from the shared `objects/`,
  which the worker can write.

- **One global deny.** Not `(allow default)` with a narrow `/Users` deny,
  which would leave host `/tmp`, `/private/tmp` and `/var` readable. It
  also covers `file-test-existence`, so `access(F_OK)` cannot confirm that
  a file name exists outside the wall.
- **ENOENT, not EPERM.** A denied path looks absent, as it does in
  bubblewrap's empty root. This is not cosmetic: git dies with `fatal:
  unable to access '~/.gitconfig': Operation not permitted` on an EPERM
  user config, but skips a missing one. `(with errno ENOENT)` is SBPL that
  Apple's own profiles use (`darwin-container-ruby-secure.sb` uses `(with
  errno EACCES)`); Apple does not document it. If a later macOS drops it, the
  profile stops compiling and the run fails. It fails closed, never open, but
  G10 then reports a failed command, not `unavailable`.
- **`/` is read whole, as one entry.** dyld aborts every process (SIGABRT,
  even `/bin/echo`) unless it can read `/` itself. That lists the root's
  entry names. Nothing under them is readable unless it is allowed.
- **Metadata on the ancestors.** `stat` works but listing and reading do
  not, on every ancestor of an allowed root: `/Users`, `$HOME`, `/private`,
  `/private/var`, `/opt`, the worktree's parents and each toolchain's
  parents. That is what `getcwd()`, `realpath()` and Python's startup need.
  The `/etc`, `/var` and `/tmp` link spellings of an allowed `/private` root
  get the same, because libc stats `/etc/localtime` and its target by those
  names. Without them the run silently falls back to UTC.
- **The masks still land last.** The secret floor, the store when the
  worker holds no key, and `/private/etc`'s `master.passwd`, `sudoers`,
  `sudoers.d`, `ssh` and `kcpassword` come after every allow, so no
  `extra_read_paths` entry reopens them.
- **Canonical paths.** Every root is resolved: `/tmp` is `/private/tmp`,
  `/var` is `/private/var`. Seatbelt matches the vnode's path, so the
  firmlink spelling `/System/Volumes/Data/Users/...` and
  `/Volumes/Macintosh HD/Users/...` reach the same denial. The tests probe
  both. Seatbelt also matches without case on APFS: an extra path
  `/USERS/<me>` passed the check and reopened the home (review round 2,
  M1). The checks on `extra_read_paths` and the detected roots now compare
  `secret_floor.alias_key` spellings (case folded, data-volume prefix
  dropped) against the home, the skep home and `/etc` resolved
  (`/private/etc`), with both OSes' hidden entries.

### What a strict run reads on macOS (I8, stated plainly)

- `/usr` (with `/usr/local`, so Intel Homebrew), `/System` but
  `/System/Volumes`, `/bin`, `/sbin`, `/dev` (device nodes keep their Unix
  permissions; the raw disks are root/operator only) and `/opt/homebrew`.
  Homebrew's `var/` (`/opt/homebrew/var`, `/usr/local/var`: postgres,
  mysql and redis data dirs) is masked last; its `etc/` stays readable
  (below).
- All of `/Library`. That includes `/Library/Keychains/System.keychain`
  (encrypted; its key is root-only), system-wide preferences and
  `/Library/Application Support`, and the CommandLineTools. It holds no
  user's files. It stays whole because TLS trust, Apple's git and system
  frameworks all read from it.
- `/private/etc`, minus the auth entries above.
- Three spots in `/private/var`: `select/` (the links `/bin/sh` and
  xcode-select read), `db/timezone/` (where `/etc/localtime` points) and
  `run/resolv.conf`; and the `db/xcode_select_link` link itself.
- The active developer dir. `/usr/bin/git` is xcrun's shim and runs git from
  it. When that dir is Xcode, the whole `Xcode.app` is read, because xcrun
  stats its `Info.plist` and loads its `SharedFrameworks`.
- `strict_read_roots()`, the git metadata, the writable roots (worktree,
  results, the run-private `TMPDIR` inside it, caches) and
  `extra_read_paths`, exactly as on Linux.

Not readable: the rest of `$HOME` (`~/Downloads`, `~/Documents`,
`~/Library`, `~/.gitconfig`), `/Users/Shared`, other users' homes, host
`/tmp` (`/private/tmp`), `/private/var/folders` (every per-user temp and
cache dir), the rest of `/private/var`, `/Volumes`, `/Applications` apart
from the Xcode bundle, `/opt` apart from Homebrew, and `/nix`. A toolchain
under one of these, such as MacPorts' `/opt/local`, Nix, or an app's bundled
CLI, needs `extra_read_paths`. A `~/.local/bin` link into a dir outside the
detected toolchains fails as missing, and a shell's PATH search moves on to
the next match. xcrun still prints `couldn't create cache file
'/var/folders/.../xcrun_db'`. That write was already denied before; only its
errno changed.

Proven by `tests/supervisor/test_sandbox_read_confinement.py` against real
`sandbox-exec`: the canary probe in the worker profile and in the G10
profile, plus Python (`import skep`, `getcwd`, `realpath`), `git status`
under the real `~/.gitconfig`, `uv run pytest`, node and the local timezone
in the worker wall. The same tests run on Linux against bubblewrap.

## Not covered yet

- Homebrew's `etc/` (`/opt/homebrew/etc`, `/usr/local/etc`) stays readable
  on a Mac: its service configs can hold a password (`redis.conf`
  `requirepass`, a `my.cnf`), but Homebrew's OpenSSL reads its CA bundle
  from there (`etc/openssl@3/cert.pem`), so masking it would break TLS for
  Homebrew's Python and curl. A project that needs it closed names
  narrower paths itself, or runs denylist-only.
- Under Seatbelt, `git log --all` and `git prune` stop on the unreadable
  `refs/stash` while the operator's checkout has a stash (M3, above). The
  stash's objects are readable by hash anyway (`git fsck --unreachable` in
  the wall lists them; `objects/` is shared), so the exclusion hides the
  ref, not the content.

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
- G10 checks the recorded targets once, when it starts. Bubblewrap binds by
  path, so a target swapped for a link while that G10 is still running
  would be followed.
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
- On Macs, new sandboxed projects read strictly from 2026-11-23. Existing
  ones keep denylist-only reads until the operator runs the doctor's
  command, the same rule as on Linux.
