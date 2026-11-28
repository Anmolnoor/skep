# ADR 0068 — strict read confinement: a sandboxed run reads an allowlist

**Status:** accepted (Phase 03, Work Item 3.2). Linux bubblewrap, the G10
re-verify and the Queen's file reads are done. The macOS Seatbelt half was
added on 2026-11-23 (see "The Seatbelt half"). The git metadata a strict run
reads was widened again the same day, so git in the wall cannot prune other
checkouts' objects (see "Data safety over hidden pointers"), and is now the
common dir itself, bound live (see "Live git metadata, the worker's own
wall", review round 3, 3I). The fast lane and `diagnose` re-runs joined the wall in
v135 Wave 1 (see "The fast lane and diagnose re-runs join the wall").
**Related:** ADR 0066 (new projects sandboxed by default; this ADR copies its
rollout rule), ADR 0047 (external engines: the sandbox is their only wall),
ADR 0062 (provider-key relay). Invariants touched: I4, I5, I8, I9, I11, I12.

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
     `/home`, `/var/home`), in or over the skep home, on `/etc`, a dir that
     holds it (`/private` on macOS) or its hidden entries, or in Homebrew's
     `var/` (masked on macOS) refuses the run
     (`sandbox.extra_read_path_problem`); setup refuses the same paths first,
     naming a subdirectory to use. A written
     root that is a symlink (uv's `cpython-3.12-…` minor-version link, which a
     venv's python points through) is recreated as a link to its resolved
     root.
   - the run's git metadata: the common dir itself, read-only and live (a
     file the operator's git replaces by rename stays current, 3I below),
     with `objects/` and the run's own git dir bound writable on top. Inside
     it, the main checkout's own state no git command in the run reads,
     `COMMIT_EDITMSG`, `MERGE_MSG` and `ORIG_HEAD`, is masked. `refs/`,
     `logs/` (the stash and its reflog included), `packed-refs`, the main
     `index` and `worktrees/` (the sibling runs' HEAD, index and reflogs)
     are readable, because git's reachability walk reads them (2026-11-23,
     below); one of those that is a link out of the common dir refuses a
     strict run. `git status`, `git diff` and `git log` still run.
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
   that now points at the home refuses the run as `sandbox_unavailable`),
   and only those that exist, as the worker's profile binds them (3I).
   The key is absent for a denylist-only run. A worker that had no wall
   (workspace mode, or the sandbox off) records the same list as
   `g10_read_allowlist` instead, so its record never claims a wall its
   worker did not have (I8); G10 still re-runs that patch inside the
   project's wall, narrower than what the worker read. `reverify_run`, live
   or after a restart, reads that record and never the policy again, and
   binds exactly those targets: one that is now a link, or no longer
   resolves to itself (a parent swapped for a link), makes G10
   `unavailable`, naming the path and where it leads, and nothing runs. A
   recorded wall with the sandbox off makes G10 `unavailable` too (3I): it
   never runs on the host. So
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
   available; N of M sandboxed projects strict; forged tools, script
   schedules and the fast lane always strict; diagnose re-runs take their
   run's recorded strict setting and extra read paths, plus today's system
   and toolchain dirs; MCP servers you register run on the host,
   unsandboxed` (v135 wording; see "The fast lane and diagnose re-runs join
   the wall")
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
- The repo's shared git metadata, the common dir whole: `config`, which git
  cannot run without and which can carry credentials (an
  `http.extraheader`, a token in a remote URL); every ref and reflog, the
  stash's included, `packed-refs`, `hooks/`, `info/`; the operator's
  staging `index` (which paths they staged); the main checkout's
  `MERGE_HEAD` and `AUTO_MERGE` (commit ids); and the sibling runs' git
  dirs under `worktrees/` (their HEAD, index, reflogs and in-progress
  messages), never their checkouts. It is live, so a file made there during
  the run shows too. `COMMIT_EDITMSG`, `MERGE_MSG` and `ORIG_HEAD` are
  masked: on bubblewrap only those present when the wall starts, so one the
  operator's git creates later, or an `ORIG_HEAD` it replaces by rename,
  shows through; Seatbelt denies the three by name. `objects/` is shared
  too: every object in it is readable by hash, including blobs only a
  sibling run, the operator's staged index or a stash ever referenced.
- With a dev checkout's serve, skep's own interpreter and package roots are
  that checkout's `.venv` and `src/`, so a strict run reads that much of the
  operator's checkout.
- Its own worktree, results dir and caches, read and write.
- The **names** of masked credential paths that exist, never their contents.
  The masks are laid down even where nothing is bound, so `~/.bashrc` shows
  up as an unreadable placeholder and `~/.ssh` as an empty dir.

Not readable: the rest of `$HOME`, other users' homes, `/var`, `/mnt`,
`/media`, `/srv`, `/root`, `/boot`, `/etc/pve`, sibling runs' worktrees (the
checkouts), other runs' results and audit trails, other managed repos, the
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
  for-each-ref` and `git branch` found nothing and exited 0. It was then
  listed (names only), with `refs/stash` left unreadable, so git called the
  stash a broken ref and `git log --all` and `git prune` stopped on it.
  Since 2026-11-23 `refs/` is bound whole (below), stash included, and the
  listing rule is gone with the reason for it.

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
- `objects/` is still writable, so a worker that deletes object files by
  hand (`rm`, not git) still destroys other checkouts' data. Git in the wall
  no longer does it by accident, with one race left: an ordinary `git
  prune` (its default expiry is now) in one run can delete a blob or tree a
  sibling run, or the operator, has just written, before that sibling's
  `git add` or `git commit` saves the index or HEAD that points to it. A
  store the worker cannot delete from, a per-run object dir with the shared
  one as a read-only alternate, closes both.
- A unix socket made in the common dir during a run (git's fsmonitor daemon,
  started after the wall) is not masked: the masks are laid when the wall
  starts. Landlock's unix-socket scoping still refuses a connect outside the
  writable roots where the kernel offers it (R5b).
- On bubblewrap, a masked `MERGE_MSG` that the operator's git deletes
  between the profile write and the wall's start (their merge completes)
  stops bwrap from starting: it cannot make the mountpoint in the read-only
  common dir. The run fails closed, but G10 then reports a failed command,
  not `unavailable`, as it does for a secret-floor file deleted in that
  window.

- MCP servers the operator registers were never in a sandbox at all: they
  run on the host as the operator, by design (I5 decides their calls).
  Forged tools moved to the strict wall in Phase 07, the fast lane and the
  `diagnose` re-run in v135 Wave 1 (both below).
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

## Data safety over hidden pointers (2026-11-23, review round 3)

The narrowed git metadata (review round 1, W4, and the `worktrees/`
exclusion) left `objects/`, which every checkout of the repo shares,
writable. So the walk git uses to decide what is garbage ran on a partial
view. In a strict run, on real bubblewrap, `git prune` in the run's worktree
deleted both of the operator's stashes (`stash@{1}` lives only in the
stash's reflog), a blob the operator had staged and not committed, a sibling
run's staged blob, and the commit at that sibling's HEAD.
`git repack -a -d` deleted the same five after they were packed. Before
Phase 03, `--ro-bind / /` showed git all of it, so this was a regression.
`git gc` itself dies first, on `gc.pid.lock` in the read-only common dir.

Git's walk reads every ref (`refs/stash` included), every reflog (the
stash's included), the index of every worktree (the main checkout's and each
sibling's), and each sibling worktree's HEAD and reflogs from
`worktrees/<id>/` (it finds them through `worktrees/<id>/gitdir`). It does
not read `ORIG_HEAD`, `MERGE_HEAD` or `AUTO_MERGE`: host git prunes an
object only they name, so `ORIG_HEAD` stays hidden, with the in-progress
messages. (`MERGE_HEAD` and `AUTO_MERGE` were bound all along, as entries
no rule skipped; 3I says so in the readable list.)

So `refs/`, `logs/`, the main `index` and `worktrees/` are bound whole and
read-only again, on every backend, from the one list in
`worktree.git_metadata_readable_roots`. The run's own git dir is a writable
root bound on top. `worktrees/` is bound as a directory, not entry by entry,
so a run started after this one is in the walk too. A symlink inside these
dirs resolves inside the wall, where the host's files are absent, so W4's
rule (never bind a symlinked entry by name) still holds for the entries
themselves. On Seatbelt, `refs/` is now read whole, so the M3 listing rule
went away.

What the run can now read that it could not: the operator's stash commits
and the paths in their index, and the sibling runs' HEAD commits, staged
paths and commit messages. Hiding them protected little. The objects they
point to were already readable by hash (`git fsck --unreachable` in the
wall listed them), while hiding the pointers let any `git prune` delete
other people's work. Writes are still refused: a sibling's `HEAD`, the
operator's `index` and `refs/stash` are read-only in the wall, and `git
worktree prune` there sees the siblings as prunable (their checkouts are
outside the wall) but fails to remove them. A config that refuses pruning
was rejected: `git prune` ignores `gc.pruneExpire`, `git repack -a -d`
ignores both gc keys, and a worker controls its own environment.
Invariants: I8 (the readable list above is updated), I11 (the operator's
data), I12.

Proven by `tests/supervisor/test_sandbox_read_wall_r3.py` against real
bubblewrap (the same test runs under Seatbelt on a Mac): `git prune` and
`git repack -a -d` in the wall, then the stashes, the staged blobs and the
sibling's HEAD are still there on the host.

## Live git metadata, the worker's own wall (2026-11-23, review round 3, 3I)

- **The common dir is bound as a directory.** 3H bound `index` and
  `packed-refs` (and `HEAD`) as files. git replaces each by rename, so a
  file bind kept the dispatch-time inode, and a `packed-refs` made after
  dispatch (`git pack-refs --all`, gc, a fetch) was not bound at all. On
  real bubblewrap, after the operator's `git add` or `git pack-refs --all`
  during a run, `git prune` in that run's wall deleted the new blob, or the
  commit only the packed tag named, from the shared `objects/`
  (`tests/supervisor/test_sandbox_read_wall_r3.py`, the running-wall test;
  host `git fsck` then failed). `git_metadata_readable_roots` now returns
  the common dir itself; bubblewrap binds it read-only, then `objects/` and
  the run's own git dir writable on top, then the masks
  (`worktree.git_metadata_masked`) last. Seatbelt matches by path, so a
  replaced `index` was live there already, but a `packed-refs` made after
  the profile was written had no allow rule and read as missing; it now
  allows the dir as one subpath and denies the three masked names. A symlinked entry is no longer dropped by name: it
  stays a link inside the bind and resolves inside the wall, where its
  target is absent unless bound (W4 still holds).
- **A linked walk entry refuses a strict run.** `refs`, `logs`, `index` or
  `packed-refs` that is a link out of the common dir resolves to nothing in
  the wall, so `git prune` there would walk without it. `write_profile`
  raises `SandboxUnavailableError` naming the link and its target
  (dispatch records `sandbox_unavailable`, G10 `unavailable`). A link that
  stays inside the common dir is bound with it and runs. A linked
  `worktrees/` already stops git itself: the run's gitfile no longer finds
  its common dir.
- **G10 replays the worker's effective extra roots.** Dispatch recorded
  every judged extra path, including one that did not exist, while the
  worker's profile skips a missing one. Made before G10, G10 bound it, so
  G10 read wider than the worker (real bubblewrap: the late canary was
  read). Dispatch now records only the targets that exist.
- **A recorded wall never runs on the host.** With the sandbox off, G10
  ignored both recorded fields and ran the patch's verify unconfined.
  `reverify_run` now reads the record before anything else, and a recorded
  `read_allowlist` or `g10_read_allowlist` with the sandbox off makes G10
  `unavailable`, saying how to fix it. A backend that cannot hold the wall
  already did the same.
- **Extra paths Seatbelt cannot honour are refused.** One in Homebrew's
  `var/` was accepted, then masked last on a Mac (ENOENT). `/private` (or
  `/PRIVATE`, or its data-volume spelling) passed the `/etc` check, which
  compared equality only, and would have reopened host `/tmp`, `/private/var`
  and `/private/etc`. A dir that holds `/etc` is now refused, on both OSes'
  checks, as are the detected roots.

Invariants: I2 (G10 never trusts a worker-era record it cannot enforce),
I8 (the readable list above is updated), I9 (each refusal names the path
and the fix), I11 (the operator's staged and packed data), I12 (unavailable,
never a quiet host run). Proven against real bubblewrap by the running-wall
test (the same test runs under Seatbelt on a Mac) and `test_g10_never_reads_an_extra_root_made_after_dispatch` in
`test_sandbox_read_wall_r2.py`.

## Review round 4 (2026-11-23)

- `ORIG_HEAD` is no longer masked: a `/dev/null` over it made in-wall
  `git fsck` exit 8 (`badRefFiletype`), and the commit it names is already
  reachable through `objects/` and `logs/`. Only `COMMIT_EDITMSG` and
  `MERGE_MSG` stay masked (where they exist as regular files at start; a
  symlinked one is skipped, since bwrap cannot mount over a link). Earlier
  sections that list `ORIG_HEAD` among the masked files describe round 3.
- `HEAD` joins the entries whose link out of the common dir refuses a strict
  run, beside `refs`, `logs`, `worktrees`, `index` and `packed-refs`.
- A recorded extra read root that vanished between dispatch and profile time
  refuses the wall (`sandbox_unavailable` for the worker, `unavailable` for
  G10) rather than quietly building a narrower wall than the run recorded.
- The Homebrew `var/` refusal for extra paths applies on macOS only;
  bubblewrap honours those paths.

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

## Forged tools join the wall (Phase 07, 2026-11-23)

Work item 7.1 closed the deferral above for forged tools. Every exchange
with a forged or seed tool — discovery (`tools/list`, the serve-start warm
included) and each `tools/call` — and both stages of its promotion trial and
its dependency install lane now run through one launch path,
`mcp_client.forged_lane`, with `read_allowlist=()`: the system dirs, the
detected toolchains (which include `<skep home>/tools/.venvs`, where a
locked tool's venv lives, read-only) and the lane's own throwaway
workspace. The tool's approved source is copied into that workspace, so not
even its installed copy under `<skep home>/tools` is read in place. Egress
replaced `ALLOW_ALL_NETWORK`: deny-all unless the tool's promotion card
approved hosts (`ForgedPlugin.approved_hosts`), then pinned to those hosts
through a FilteringProxy.

What did not move, and why:

- **The fast lane and `diagnose` re-runs** use their own launch paths
  (`fastlane.py`, `actions.diagnose`), not the forged-tool lane. `diagnose`
  re-runs a kept worktree and should take the run's recorded wall, as G10
  does; the fast lane runs a throwaway script. Each is its own change.
- **Operator-registered MCP servers** (`register_mcp_server`, `/browser`)
  run on the host, unsandboxed, as the operator. Putting them in a wall is
  a product decision (Playwright needs its browser, a mail server its
  credentials), not a profile flag.
- **The dispatched half of the trial** keeps the Queen's role chain (R3-F2)
  and nothing else: since the Phase 07 review (R1) it runs a one-line gate
  script, never the candidate. Every execution of a forged tool — its two
  trial stages, its install lane, discovery and every call — runs behind
  `forged_lane`, so "forged tools always strict" (the doctor line) holds
  with no exception.

The same review closed the gaps around that lane: a `forge-*` server id is
reserved before any transport is chosen (an `http` config with that prefix
runs the bound record through the lane, never a host-side request, and
`register_mcp_server` refuses the prefix); a locked tool's venv is sealed
(`forge.seal_venv`: every path, file digest and link target, with only
`bin/python*` allowed to link out of the tree) when its hashed install
finishes, the seal is recorded with the approval (`venv_sha256`) and
checked before every reuse and every launch; and the promotion publishes
its binding atomically — conditional on the plugin record it read before
the trial, with the previous tool's bytes restored if the publish fails.

### Review round R2 (Phase 07, 2026-11-23)

- **The venv that launches is the venv that was sealed, as far as skep can
  make it.** A promotion checks the staged venv against its seal *before*
  it replaces anything, keeps the previous venv until the moved tree still
  matches (and puts it back if it does not), and never leaves a staging
  tree behind a failure. The committed tree is read-only (every write bit
  off; the seal covers paths, bytes, exec bits and link targets, not write
  bits, so this does not change it), and the forged-tool wall binds
  `<skep home>/tools/.venvs` read-only on both backends (only the install
  lane gets its own staging dir writable).
- **The residual, stated (I8).** The seal is checked on the live tree and
  the interpreter is then launched by path: a check-then-exec gap remains.
  Closing it would mean copying the venv into each call's throwaway
  workspace. Whoever can win that race can already write the skep home
  (the tree is owned by the operator and read-only), and that writer can
  also rewrite the plugin record, so the gap gives an attacker nothing
  they did not already have. The read-only bits stop accidents and other
  processes, not the home's owner.
  ponytail: the seal is re-hashed on every launch; cache it per
  (inode, mtime, size) if call volume on a large venv ever matters.
- **Promotion and every record write share one lock.** `promote_tool`
  reads the record it publishes against, and writes `draft → sandboxed`,
  under `PROMOTION_LOCK`; `save_plugin` takes the same (reentrant) lock, so
  a whole-list rewrite (a `forge_tool` record, say) never drops a publish
  or a rollback, and a rollback confirmed before the promotion took the
  lock wins.
- **A forged registration leaves only through its lifecycle.**
  `unregister_mcp_server` refuses a `forge-*` id (pointing to
  `suspend_tool`), and `promote_tool` answers "already active" only while
  the tool is registered.
- **Discovery is keyed by the binding.** A forged server's cached
  `tools/list` records the state, bound triple and venv seal it ran under;
  a listing whose binding changed mid-listing is discarded, and a cached one
  is served only while its binding still holds.
- **The trial needs a real tool.** `self_test` and `smoke_test` are probes;
  a server exposing only those fails the trial.
- **A declared dependency is one exact pin.** `name==version`, no range
  or marker. Every lock line for that project must pin that version with no
  marker, so the card's pin is what installs. A resolver (`packaging`) is
  not a skep dependency.

## Script schedules join the wall (Phase 09 R1)

A `script` schedule ran supervisor-side with the operator's whole
filesystem and unrestricted egress: its scrubbed env removed keys, not
reach. The morning-ritual skill made that concrete — a mail, calendar and
weather fetcher whose output a Queen turn then reads. Since Phase 09 R1
`scheduler.run_schedule_script` runs every script through `wall.walled`,
the launch path `forged_lane` now shares, so the two cannot drift:

- **Reads:** the strict allowlist (`read_allowlist`), plus the script's
  declared inputs (`scheduler.script_inputs`): each absolute, existing,
  non-link file its command names, as an argument or as a `NAME=value`
  value. Never a directory. Each is judged by `extra_read_path_problem`,
  so one in or over the skep home, at or above `$HOME`, or on `/etc`
  refuses the run (`script refused: …`). The wall takes them as single
  files (`read_files`, Phase 09 R2), never as read roots. Each must be
  named by an absolute path with no `..` component (judged as written,
  before anything normalizes it), lie under none of `/proc`, `/sys` and
  `/dev` (`/proc/<pid>/environ` would hand a script the supervisor's env),
  be its own real path (no link in any component) and be a regular file
  (`sandbox.open_read_file`, which opens it without following links and
  reads no bytes). The secret masks still land last, and an input that is
  a mask or under one (`sandbox.masked_input_problem`, both sides
  resolved) refuses before anything is granted. All of these run at
  every creation face and again at the run and in `write_profile`
  (Phase 09 R3, R4). A redirect such as `> /dev/null` is not an input
  (`script_inputs` takes regular files only) and keeps working.
  On both backends an input is the **live file, read-only, at its own
  path**: bubblewrap `--ro-bind`s it in place, after the writable binds
  and before the masks; Seatbelt grants its `(literal …)` path. A script
  sees the file as it is while it runs and cannot write it. A refused
  run's reason is the schedule's health state (`script_failed: script
  refused: …`).
  The card shows the command, so the operator sees every input.
- **Writes:** a throwaway workspace (its `HOME`, `TMPDIR`), deleted after
  the run.
- **Egress:** the schedule's `network` hosts through a FilteringProxy, none
  by default. The REST face, `propose_schedule` and `skep schedule add
  NAME "<command>" --caste script --network HOST` (repo-less since Phase 09
  R2, like the other two) set it. Each
  entry is one concrete host (`api.example.com`, `host:8443`); `*` alone
  would make the wall allow-all, past the proxy, so every creation face and
  `wall.walled` itself refuse it (Phase 09 R2), and since Phase 09 R3 a
  wildcard domain (`*.example.com`) refuses too (`netproxy.host_entry_problem`).
  Only a forged tool's `# skep-hosts:` may name `*.example.com`
  (`forge_host_entry_problem`), and only `forged_lane` takes it into the
  wall (`walled(domain_patterns=True)`).
  IMAP has no proxy support, so `skep.mcp_servers.mail`'s digest tunnels
  through the proxy with one CONNECT (`HTTPS_PROXY`); TLS still runs end to
  end with the mail host.
- **No backend refuses the run** (`script refused: no usable sandbox
  backend …`), a health failure like any other (I12).

What a walled script still does (I8): it runs as the operator, on this
host; it reads every file its command names and every world-readable file
the strict allowlist opens; it sends what it reads to the hosts it names,
which is its job — the proxy checks host names, not content. skep's own
secrets are out of reach, so a script that needs a password reads it from a
file of its own, outside the skep home, named in its command
(`SKEP_MAIL_PASSWORD_FILE`). An existing script that read `$HOME` or used
the network now fails until its command names those files and its schedule
names those hosts.

Phase 09 R4, scope decision SD-1: staging removed. R2–R4 copied each
input into a stage the supervisor wrote, and the supervisor reading a file
on the script's behalf, with its own authority, was the leak (R4 found it
reading `/proc` for a script); the wall now binds the operator's file
itself. **Not defended:** (a) a second hard link to a masked file, named
as an input (the mask covers the masked path, not the inode); (b) a path
swapped between the check and the bind (bubblewrap binds by path and
follows a link put there after the check). Both need code already running
as the operator outside any wall, which can read those files directly; a
walled worker, script or forged tool cannot see a masked file to link it
and cannot write the operator's directories.

## The fast lane and diagnose re-runs join the wall (v135 Wave 1, 2026-11-27)

The two lanes the Phase 07 section left behind:

- **The fast lane** (`run_code(fast=True)`, `serve/fastlane.py`) passes
  `read_allowlist=()`: the system dirs, the detected toolchains and its
  throwaway workspace. It runs only on the native backend (bubblewrap or
  Seatbelt), and both hold the wall. Serve's own interpreter is a detected
  toolchain, so a skep installed in a venv under `$HOME` (uv tool, pipx)
  still starts; the home itself is never bound. On bubblewrap a venv
  reached through a linked parent directory does not start in the wall
  (the link is not recreated), and the wall is not widened for it.
- **`diagnose_run`** (`serve/actions.py`) takes the strict setting and the
  resolved extra read paths the run's `dispatched` line recorded, through
  `reverify._recorded_read_wall`, the reader G10 uses; the system and
  toolchain dirs are detected again when the diagnosis runs, as they are
  for G10, so after a serve restart on another interpreter or toolchain
  setup that part of the wall can differ from the worker's. It refuses as
  G10 does: a recorded root that is a link now (`_moved_read_roots`)
  refuses before any profile is written, and a strict wall the backend
  cannot hold refuses through `write_profile`. A run that read
  denylist-only is diagnosed denylist-only. The git metadata it binds is
  held to the run's recorded repo (`worktree.kept_tree_git_dirs`): the
  kept tree is the worker's output, so a `.git` pointer or `commondir`
  leading anywhere else refuses the diagnosis. Both refusals are asked
  when the card would be offered and again at confirm, and the profile
  masks the in-progress message files of the common dir that check held,
  not of whatever the tree points at by then.

The tests assert, on real bubblewrap and on real Seatbelt (CI's macOS job),
that each lane reads none of four home canaries
(`~/Downloads`, `~/Documents`, `~/.ssh`, a sibling repo) while a Python
script and a `pytest` run still pass
(`tests/supervisor/test_v135_strict_reads_remaining.py`). Operator-registered
MCP servers are still unconfined; ADR 0069 is that change.
