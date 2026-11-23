"""G10: supervisor-side re-verification — evidence over claims.

v1 trusted the worker's verification claim. v2 re-runs it: apply the worker's
patch to a *clean* worktree at the same baseline, re-run the recorded
verification command(s) under the host sandbox backend, and compare. A worker that
claims ``completed``/``passed`` but whose patch fails re-run is caught here, and
the disagreement is recorded loudly (and, in Stage C, blocks auto-approval).

This is a ~plain validator, not a third agent: it runs a recorded command
independently and trusts only the exit code.

v88-F4: *which* command is now the supervisor's call when the project says so.
Before, re-verification re-ran whatever the worker nominated as its verify step
— so a worker whose verify command was ``true`` earned ``confirmed=True`` and,
under a ``require_reverified`` auto-approval rule, an automatic landing. Trusting
only the exit code is worthless if the claim under test picks the command. A
project's pinned ``verify_command`` (policy overlay) wins; unset falls back to
the worker's nomination, and the outcome detail always says which was used (I8).
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass, replace
from fnmatch import fnmatch
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from skep.worker_contract import Event, EventType

from . import sandbox
from .apply import patch_paths, resolve_commit
from .config import SupervisorConfig
from .netproxy import FilteringProxy, control_plane_ports, run_loopback_ports
from .policy import test_config_changes
from .spawner import proxy_env
from .store import RunStore
from .worktree import (
    TREE_LOCK,
    _resolve_ref,
    create_worktree,
    git_metadata_readable_roots,
    git_metadata_writable_roots,
    remove_worktree,
)

# pytest/usage exit codes: 0 pass, 1..4 real failure, 5 no-tests; 127 = command
# not found (the supervisor lacks the worker's toolchain — can't re-verify here).
_COMMAND_NOT_FOUND = 127
_REVERIFY_TIMEOUT_SECONDS = 300.0


# V1 (phase-01 1.3): what the guard can and cannot see, said on every verdict.
V1_SCOPE = (
    "V1 detects deleted or emptied test files, added skip/xfail markers, test-config "
    "edits and drops in the executed test count; it cannot prove an edited assertion "
    "is still strong"
)


@dataclass(frozen=True)
class ReverifyOutcome:
    """One G10 verdict.

    V1 (phase-01 1.3): ``tamper_flags`` non-empty makes the outcome
    "tampered" and the run unconfirmed. V1 detects deleted or emptied test
    files, added skip/xfail markers, test-config edits and drops in the
    executed pytest count; it cannot prove an edited assertion is still strong.
    """

    # "passed" | "failed" | "unavailable" | "not_applicable" (v65-F1: a run
    # that claimed no file changes has no patch to re-verify — benign, never
    # the lying-worker shape) | "tampered" (V1: the patch changed the tests).
    outcome: str
    commands: list[str]
    exit_codes: list[int]
    detail: str
    # v88-F4 (I8): "project" when the commands came from the project's pinned
    # verify_command, "worker" when they came from the worker's own
    # verify.result event. The record must say what it actually re-ran.
    commands_source: str = "worker"
    # V1: "kind" or "kind:path" — test_file_deleted, test_file_emptied,
    # skip_marker_added, test_config_changed, test_total_dropped,
    # test_passed_dropped, test_skipped_increased, test_xfailed_increased,
    # tamper_check_failed (the guard itself could not read the patch).
    tamper_flags: tuple[str, ...] = ()
    # V1: see ReverifyRecord.test_counts.
    test_counts: dict[str, Any] | None = None


# V1: pytest's own summary line ("== 1 failed, 2 passed in 0.12s ==", or
# "no tests ran in 0.01s"), read from the pin's output — the pin is never changed.
_PYTEST_SUMMARY = re.compile(r"^=*\s*((?:\d+ [a-z]+(?:, )?)+|no tests ran)\s+in\s+[\d.]+\s*s\b")
_PYTEST_WORDS = {
    "passed": "passed",
    "failed": "failed",
    "error": "errors",
    "errors": "errors",
    "skipped": "skipped",
    "xfailed": "xfailed",
    "xpassed": "xpassed",
    "deselected": "deselected",
}
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
# pytest exits 0 (pass), 1 (failures) or 5 (no tests) when the tests ran.
_COUNTABLE_EXITS = (0, 1, 5)
# Review R1 (F4): a segment led by one of these only names pytest.
_NOT_RUNNERS = frozenset({"echo", "printf", "grep", "rg", "which"})


# pytest, py.test, pytest-3 (Fedora/Debian); grouped flags like -mpytest, -Impytest.
_PYTEST_WORD = re.compile(r"^py\.?test(-3(\.\d+)*)?$")
_PYTEST_FLAGS = re.compile(r"^-[A-Za-z]*m(pytest|py\.test)$")


def _is_pytest_pin(command: str) -> bool:
    """V1 (review R1, F4): does the pin run pytest? Read from shell words —
    quotes undone, `;` `&&` `||` `|` split off, an `sh -c '...'` program read
    too — so `pytest;`, `'pytest'` and `"python" -m pytest` count. A segment
    led by `echo`/`grep`/... only names it. A false positive fails closed
    (unreadable counts block auto-landing); a miss would skip the check."""
    lexer = shlex.shlex(command, posix=True, punctuation_chars="();<>|&\n")
    lexer.whitespace = " \t\r"  # a newline separates commands, like `;`
    lexer.whitespace_split = True
    lexer.commenters = ""  # review R3 (H4): `FOO=a#b pytest` is no comment
    try:
        words = list(lexer)
    except ValueError:
        return "pytest" in command or "py.test" in command
    segment: list[str] = []
    for word in [*words, ";"]:
        if word and all(c in "();<>|&\n" for c in word):
            while segment and (segment[0] == "env" or _ENV_ASSIGNMENT.match(segment[0])):
                segment.pop(0)
            if segment and segment[0] not in _NOT_RUNNERS:
                for w in segment:
                    if _PYTEST_WORD.match(os.path.basename(w)) or _PYTEST_FLAGS.match(w):
                        return True
                    # an `sh -c` program, spaced or not (`sh -c "pytest;true"`)
                    if (
                        w != command
                        and any(c.isspace() or c in ";&|<>()'\"" for c in w)
                        and (_is_pytest_pin(w))
                    ):
                        return True
            segment = []
        else:
            segment.append(word)
    return False


def pytest_counts(output: str) -> dict[str, int] | None:
    """The counts on pytest's summary line, or None when there is none.
    ``total_executed`` = passed + failed + errors + xfailed + xpassed.

    Review R1 (F2, F9): a line counts as a summary only with a pytest word in
    it ("2 files in 0.01s" is not one); more than one summary — a pin that
    runs pytest twice, a fake printed after the real one — is unreadable,
    never "the last one wins"."""
    found = []
    for line in _ANSI.sub("", output).splitlines():
        match = _PYTEST_SUMMARY.match(line.strip())
        if match is None:
            continue
        pairs = re.findall(r"(\d+) ([a-z]+)", match.group(1))
        if match.group(1) != "no tests ran" and not any(w in _PYTEST_WORDS for _, w in pairs):
            continue
        counts = dict.fromkeys(dict.fromkeys(_PYTEST_WORDS.values()), 0)
        for number, word in pairs:
            if word in _PYTEST_WORDS:
                counts[_PYTEST_WORDS[word]] += int(number)
        counts["total_executed"] = sum(
            counts[k] for k in ("passed", "failed", "errors", "xfailed", "xpassed")
        )
        found.append(counts)
    return found[0] if len(found) == 1 else None


def count_flags(baseline: dict[str, int], patched: dict[str, int]) -> list[str]:
    """V1: tests the patch made vanish or stop counting. A passing test that
    turned into a failing one is a regression (the re-run fails), not tamper."""
    flags = []
    if patched["total_executed"] < baseline["total_executed"]:
        flags.append("test_total_dropped")
    if patched["passed"] < baseline["passed"] and (
        patched["failed"] + patched["errors"] <= baseline["failed"] + baseline["errors"]
    ):
        flags.append("test_passed_dropped")
    if patched["skipped"] > baseline["skipped"]:
        flags.append("test_skipped_increased")
    if patched["xfailed"] > baseline["xfailed"]:
        flags.append("test_xfailed_increased")
    return flags


_SKIP_MARKER = re.compile(
    r"pytest\.mark\.(?:skip|xfail)|\bpytest\.(?:skip|xfail)\(|\b(?:it|test|describe)\.skip\b"
    r"|#\[ignore\]|\bt\.Skip\("
)
_DOC_SUFFIXES = (".md", ".rst", ".txt")


def _is_test_file(path: str) -> bool:
    parts = PurePosixPath(path).parts
    name = parts[-1] if parts else ""
    return (
        "tests" in parts[:-1]
        or (name.startswith("test_") and name.endswith(".py"))
        or any(fnmatch(name, g) for g in ("*_test.*", "*.test.*", "*.spec.*"))
    )


def static_tamper_flags(repo: Path, patch_path: Path, base: str) -> list[str]:
    """V1: what the patch does to the tests, read by the supervisor from the
    patch artifact applied to ``base`` in a scratch index (never the worker's
    claim). Test-config edits are the test-runner files alone, compared
    section by section where the landing gate's parser can (review R1, F7):
    a dependency bump, a vite.config or a setup.py edit is the landing gate's
    concern (pin-control), not test tampering.

    Review R1 (F5): paths are read NUL-delimited and blobs as bytes, so a
    quoted path or a binary fixture can't hide a deletion or crash the
    verdict; anything git or a parser raises is a flag, never an escape."""
    try:
        return _static_tamper_flags(repo, patch_path, base)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return [f"tamper_check_failed:{type(exc).__name__}"]


def _static_tamper_flags(repo: Path, patch_path: Path, base: str) -> list[str]:
    changed = patch_paths(repo, patch_path)
    if not changed:
        return []
    with tempfile.TemporaryDirectory(prefix="skep-v1-") as tmp:
        # Literal pathspecs: a test path with `*` or `[` names itself only.
        env = {
            **os.environ,
            "GIT_INDEX_FILE": str(Path(tmp) / "index"),
            "GIT_LITERAL_PATHSPECS": "1",
        }

        def git(*args: str) -> subprocess.CompletedProcess[bytes]:
            return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, env=env)

        if (
            git("read-tree", base).returncode
            or git("apply", "--cached", str(patch_path)).returncode
        ):
            # the re-run already says the patch does not apply
            return [
                f"test_config_changed:{p}"
                for p in test_config_changes(repo, patch_path, base, changed)
            ]
        fields = git("diff", "--cached", "--no-renames", "--name-status", "-z", base).stdout
        names = [f.decode("utf-8", "surrogateescape") for f in fields.split(b"\0") if f]
        # Review R3 (H3): the config scan reads these unquoted names too — a
        # git-quoted `.github/workflows/é.yml` matched no prefix.
        flags = [
            f"test_config_changed:{path}"
            for path in test_config_changes(repo, patch_path, base, tuple(names[1::2]))
        ]
        for code, path in zip(names[::2], names[1::2], strict=True):
            if _is_test_file(path):
                if code == "D":
                    flags.append(f"test_file_deleted:{path}")
                elif code == "M" and not git("show", f":{path}").stdout.strip():
                    flags.append(f"test_file_emptied:{path}")
            if code == "D" or path.endswith(_DOC_SUFFIXES):
                continue
            diff = git(
                "diff", "--cached", "--no-color", "--no-ext-diff", "-U0", base, "--", path
            ).stdout.decode("utf-8", "replace")
            hunks = diff.split("\n@@", 1)[1:]  # the header is git's, not the patch's
            if hunks and any(
                line.startswith("+") and _SKIP_MARKER.search(line) for line in hunks[0].splitlines()
            ):
                flags.append(f"skip_marker_added:{path}")
    return list(dict.fromkeys(flags))


def verification_commands_from_events(events: list[Event]) -> list[str]:
    """Pull the recorded verification command(s) out of the verify.result event (G10)."""
    for event in events:
        if event.type is EventType.VERIFY_RESULT:
            raw = event.payload.get("commands")
            if isinstance(raw, list):
                return [str(item) for item in raw]
    return []


_UV_PRIME_INPUTS = frozenset({"uv.lock", "pyproject.toml"})
# v127-A0 review (council round 2, I12): a prime has the network, so it
# reaches the package indexes alone — these, plus the hosts the BASELINE
# lockfile already resolves from — through the filtering proxy (D1).
_PYPI_HOSTS = ("pypi.org", "files.pythonhosted.org")
_NPM_HOSTS = ("registry.npmjs.org", "registry.yarnpkg.com")


def _lock_hosts(worktree: Path, lockfiles: Iterable[str], pattern: str) -> set[str]:
    hosts: set[str] = set()
    for lockfile in lockfiles:
        with contextlib.suppress(OSError):
            text = (worktree / lockfile).read_text(encoding="utf-8")
            hosts |= {urlsplit(url).hostname or "" for url in re.findall(pattern, text)}
    return hosts - {""}


def _needs_uv_priming(worktree: Path, commands: list[str]) -> bool:
    """v94-F7: does this re-verify invoke uv in a repo that defines a Python
    project? Then the venv must be primed first — under the deny-all profile
    uv can neither init its cache (`~/.cache/uv` is not writable) nor sync,
    so the default pin (`uv run pytest`, v91-F1) exited 2 for every good
    patch on a sandboxed supervisor (field run 019f9ea0)."""
    return _pin_uses(commands, frozenset({"uv"})) and (worktree / "pyproject.toml").is_file()


# v127-A0 (O12): a Node pin needs node_modules, which a fresh worktree never
# has. Which installer is decided by the lockfile (first match wins); every
# one installs exactly what the lockfile pins, lifecycle scripts off, so no
# package's code runs while the (registry-only) network is open.
# ponytail: yarn 2+ (berry) rejects --frozen-lockfile/--ignore-scripts, so its
# prime fails and re-verify says unavailable; add `--immutable --mode=skip-build`
# when a berry repo is pinned.
_NODE_PACKAGE_MANAGERS = frozenset({"npm", "npx", "pnpm", "yarn"})
# `node` too: a `node --test` pin imports the same dependencies `npm test` does.
_NODE_TOOLS = _NODE_PACKAGE_MANAGERS | {"node"}
_NODE_INSTALLS = (
    ("pnpm-lock.yaml", "pnpm install --frozen-lockfile --ignore-scripts --ignore-pnpmfile"),
    ("yarn.lock", "yarn install --frozen-lockfile --ignore-scripts"),
    ("package-lock.json", "npm ci --ignore-scripts"),
    ("npm-shrinkwrap.json", "npm ci --ignore-scripts"),
)
_NODE_PRIME_INPUTS = frozenset({"package.json", *(lockfile for lockfile, _ in _NODE_INSTALLS)})
# What a patch must not change before an install (by basename, anywhere in
# the tree): `.npmrc` can re-enable scripts or reroute the registry, a
# yarn-path, a pnpmfile or pnpm-workspace.yaml settings are code or config
# the installer obeys. `.yarn/` (yarn's releases) is matched as a prefix.
_NODE_INSTALLER_CONFIG = frozenset(
    {".npmrc", ".yarnrc", ".yarnrc.yml", ".pnpmfile.cjs", ".pnpmfile.mjs", "pnpm-workspace.yaml"}
)
_NODE_DEPENDENCY_KEYS = (
    "dependencies",
    "devDependencies",
    "optionalDependencies",
    "peerDependencies",
)
_TARBALL_URL = r"https://[^\s\"'#?,}]+\.tgz"
_YARN_PATH = re.compile(r"""^\s*(?:yarn-path|yarnPath:)\s*["']?([^"'\s]+)""", re.MULTILINE)
_SHELL_SEPARATORS = re.compile(r"&&|\|\||[;|]")
_ENV_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")


def _pin_uses(commands: list[str], tools: frozenset[str]) -> bool:
    """Does any command run one of ``tools``? Council round 3 (N6): each
    `&&`/`||`/`;`/`|` segment's first word after `env` and `VAR=val`, so
    `CI=1 npm test` and `cd web && npm test` count."""
    for command in commands:
        for segment in _SHELL_SEPARATORS.split(command):
            words = segment.split()
            while words and (words[0] == "env" or _ENV_ASSIGNMENT.match(words[0])):
                words.pop(0)
            if words[:1] and words[0] in tools:
                return True
    return False


def _node_prime(worktree: Path, commands: list[str]) -> tuple[str, str] | str | None:
    """v127-A0: ``(lockfile, install command)`` when this re-verify runs a Node
    pin in a repo whose package.json declares dependencies; the reason when it
    does but there is no lockfile to install from; None when nothing needs
    priming (not a Node pin, no package.json, or no dependencies)."""
    if not _pin_uses(commands, _NODE_TOOLS):
        return None
    manifest = _manifest(worktree)
    if not any(manifest.get(k) for k in _NODE_DEPENDENCY_KEYS):
        return None
    for lockfile, install in _NODE_INSTALLS:
        if (worktree / lockfile).is_file():
            return lockfile, install
    return (
        "no lockfile to prime from — package.json declares dependencies but there is "
        "no package-lock.json, npm-shrinkwrap.json, pnpm-lock.yaml or yarn.lock"
    )


def _manifest(worktree: Path) -> dict[str, Any]:
    try:
        manifest = json.loads((worktree / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return manifest if isinstance(manifest, dict) else {}


def _node_env(cache_dir: Path, tmp_dir: Path) -> dict[str, str]:
    """Every installer's cache under one root outside the worktree (like the
    uv cache), never the operator's ~/.npm, ~/.cache or ~/Library; npm's logs
    in the re-verify tmp dir (the verify profile reads the cache only); pnpm
    copies out of its store (a hard link would let the verify step write the
    store through node_modules); and corepack never downloads a package
    manager.

    v127-A0 trial fix (Fedora V17/V18): where `yarn`/`pnpm` are corepack
    launchers (Node's default), corepack looked in skep's XDG cache, found
    nothing and, offline, could not fetch — every yarn.lock patch read
    UNAVAILABLE. It gets the host's own corepack home instead, readable but
    never writable through the wall, with its network still off and no
    "latest" lookup: only a package manager the host already has runs."""
    return {
        "COREPACK_HOME": _host_corepack_home(),
        "COREPACK_DEFAULT_TO_LATEST": "0",
        "npm_config_cache": str(cache_dir / "npm"),
        "npm_config_store_dir": str(cache_dir / "pnpm-store"),
        "npm_config_package_import_method": "copy",
        "npm_config_logs_dir": str(tmp_dir / "npm-logs"),
        "YARN_CACHE_FOLDER": str(cache_dir / "yarn"),
        "XDG_CACHE_HOME": str(cache_dir / "xdg-cache"),
        "XDG_DATA_HOME": str(cache_dir / "xdg-data"),
        "COREPACK_ENABLE_NETWORK": "0",
    }


def _host_corepack_home() -> str:
    """corepack's own lookup, on the supervisor: $COREPACK_HOME, else
    $XDG_CACHE_HOME/node/corepack, else ~/.cache/node/corepack."""
    cache = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return os.environ.get("COREPACK_HOME") or str(Path(cache) / "node" / "corepack")


def _corepack_missing(install: str, output: str, package_manager: object) -> str:
    """Why an install failed when corepack (offline) had no cached copy of the
    package manager to run, with the one command that fixes it; empty for
    any other failure."""
    # Round 7: a read-only COREPACK_HOME makes real corepack (0.34) say it
    # cannot create its cache dir for an uncached version; a writable one
    # without network says the network line.
    offline = ("Network access disabled by the environment", "Failed to create cache directory")
    if not any(text in output for text in offline):
        return ""
    tool = install.split()[0]
    pinned = isinstance(package_manager, str) and package_manager.startswith(f"{tool}@")
    version = str(package_manager).split("@", 1)[1] if pinned else "<version>"
    return (
        f"{tool} via corepack is not installed on this host "
        f"(run `corepack prepare {tool}@{version} --activate`)"
    )


def _node_install_refusal(
    worktree: Path, touched: set[str], *, package_manager: object, protected: set[str]
) -> str:
    """Why the PATCHED package.json/lockfile may not be installed; empty when
    it may. Where packages come from is the registry-only proxy's job (with
    scripts off and corepack offline); what stays here is what would change
    the installer itself: its config files, a yarn-path target, or
    ``packageManager`` (corepack would fetch and run it)."""
    config = sorted(
        path
        for path in touched
        if PurePosixPath(path).name in _NODE_INSTALLER_CONFIG
        or path.startswith(".yarn/")
        or path in protected
    )
    if config:
        return f"it changes the package manager's config ({', '.join(config)})"
    if (changed := _manifest(worktree).get("packageManager")) != package_manager:
        return f"it changes packageManager to {changed!r} (corepack would fetch and run it)"
    return ""


class _PrimeUnavailable(Exception):
    """The prime's proxy or sandbox could not be started."""


def _start_proxy(
    stack: contextlib.ExitStack,
    hosts: tuple[str, ...],
    *,
    config: SupervisorConfig,
    bridge: bool,
    loopback_ports: tuple[int, ...] = (),
) -> FilteringProxy:
    """A filtering proxy (D1) admitting ``hosts`` (and ``loopback_ports``),
    stopped by ``stack``. ``bridge`` (bubblewrap) adds the unix socket the
    netns reaches through netshim; its directory is ``stack``'s to remove
    even when the proxy never starts (its sockets bind in __init__, ES-F257)."""
    socket_path = None
    if bridge:
        from .dispatch import proxy_socket_root  # runtime: dispatch imports us

        socket_dir = tempfile.mkdtemp(prefix="skep-nx-", dir=proxy_socket_root())
        stack.callback(shutil.rmtree, socket_dir, ignore_errors=True)
        socket_path = str(Path(socket_dir) / "p.sock")
    proxy = FilteringProxy(
        hosts,
        unix_socket_path=socket_path,
        denied_loopback_ports=control_plane_ports(config.home),
        loopback_ports=loopback_ports,
    ).start()
    stack.callback(proxy.stop)
    return proxy


class _PrimeWall:
    """The prime's sandbox: writable worktree and caches, network only to
    ``hosts`` through a filtering proxy (D1) — never allow-all. Started on
    the first install (and stopped by ``stack``); the proxy env reaches the
    installers alone, never the verify command."""

    def __init__(
        self,
        stack: contextlib.ExitStack,
        hosts: tuple[str, ...],
        *,
        worktree: Path,
        profile_path: Path | None,
        writable: list[Path],
        config: SupervisorConfig,
        name: str = "prime",
        read_allowlist: tuple[Path, ...] | None = None,
    ) -> None:
        self.hosts = hosts
        self._name = name
        self._stack = stack
        self._worktree = worktree
        self._profile_path = profile_path
        self._writable = writable
        self._config = config
        self._read_allowlist = read_allowlist
        self._proxy: FilteringProxy | None = None
        self._profile: Path | None = None

    def run(self, command: str, env: dict[str, str]) -> tuple[int, str]:
        if self._proxy is None:
            try:
                self._start()
            except (
                OSError,
                sandbox.SandboxUnavailableError,
                sandbox.SandboxAllowlistUnsupported,
            ) as exc:
                raise _PrimeUnavailable(str(exc)) from exc
        assert self._proxy is not None
        proxy = proxy_env(self._proxy.port)
        # npm/pnpm/yarn read their own proxy keys too; one that ignores both
        # meets the wall (egress pinned to the proxy port) and fails closed.
        proxy["npm_config_proxy"] = proxy["npm_config_https_proxy"] = proxy["HTTPS_PROXY"]
        code, output, _stdout = _run_command(
            command, cwd=self._worktree, env={**env, **proxy}, profile_path=self._profile
        )
        return code, output

    def _start(self) -> None:
        bridge = self._profile_path is not None and sandbox.availability().backend == "bubblewrap"
        proxy = _start_proxy(self._stack, self.hosts, config=self._config, bridge=bridge)
        if self._profile_path is not None:
            self._profile = sandbox.write_profile(
                self._profile_path.with_suffix(f".{self._name}.sb"),
                workspace=self._worktree,
                extra_writable=[*git_metadata_writable_roots(self._worktree), *self._writable],
                extra_readable=git_metadata_readable_roots(self._worktree),
                network=sandbox.NetworkPolicy(allow=self.hosts),
                proxy_port=proxy.port,
                unix_socket_path=proxy.unix_socket_path,
                skep_home=self._config.home.parent,
                read_allowlist=self._read_allowlist,
            )
        self._proxy = proxy


def command_timeout_seconds(budget_seconds: float | None) -> float:
    """The re-run bound: the run's own wall-clock budget, floored at the
    historic 300s default — a small budget must never tighten G10 below what
    it has always been afforded."""
    return max(budget_seconds or 0.0, _REVERIFY_TIMEOUT_SECONDS)


def _run_command(
    command: str,
    *,
    cwd: Path,
    env: dict[str, str],
    profile_path: Path | None,
    timeout: float = _REVERIFY_TIMEOUT_SECONDS,
) -> tuple[int, str, str]:
    """``(exit code, stdout+stderr tail, stdout)`` — V1 counts read stdout
    alone, where pytest writes its summary (review R1, F2), and all of it:
    a tail could drop the real summary and keep a fake one (review R3, H1)."""
    argv: list[str] = ["/bin/sh", "-c", command]
    if profile_path is not None:
        argv = sandbox.wrap_command(argv, profile_path)
    try:
        proc = subprocess.run(
            argv,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return -1, f"timed out after {timeout:.0f}s", ""
    combined = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
    return (
        proc.returncode,
        combined.strip()[-_OUTPUT_EVIDENCE_CHARS:],
        proc.stdout or "",
    )


_OUTPUT_TAIL_CHARS = 2000
# Review R1 (F1): what survives the clean after the baseline run — the primed
# environments (uv's .venv, node_modules, the worktree-local caches) and the
# re-verify scratch dir; nothing the baseline's tests wrote.
_BASELINE_KEEP = (".venv", "node_modules", ".reverify-tmp", ".uv-cache", ".node-cache")


def _untracked(worktree: Path) -> set[str]:
    """Untracked and ignored FILES in ``worktree`` — one entry per file, so a
    file the baseline adds inside a directory the prime made is seen (review
    R3) — minus the primed environments and re-verify scratch."""
    out = subprocess.run(
        ["git", "-C", str(worktree), "ls-files", "-o", "-z", "--", "."]
        + [f":(top,exclude){keep}" for keep in _BASELINE_KEEP],
        capture_output=True,
        check=False,
    ).stdout.decode("utf-8", "surrogateescape")
    return {
        path
        for path in out.split("\0")
        if path and path.rstrip("/").split("/")[0] not in _BASELINE_KEEP
    }


# v127-A0 trial fix (V01): what the evidence file keeps — the whole output up
# to its last 200 KB, so the traceback above pytest's summary survives.
_OUTPUT_EVIDENCE_CHARS = 200_000


def _output_tail(combined: str) -> str:
    """v115-F12: what a failed re-run SAID. The first skep-maintains-skep run
    to reach G10 failed re-verification and the record held only "exit 1" —
    the pytest output had been captured and discarded (I8, I9). pytest's own
    short summary is the most useful slice; otherwise the last 2000 chars."""
    marker = combined.rfind("short test summary info")
    if marker != -1:
        line_start = combined.rfind("\n", 0, marker) + 1
        return combined[line_start:].strip()[-_OUTPUT_TAIL_CHARS:]
    return combined.strip()[-_OUTPUT_TAIL_CHARS:]


def reverify(
    *,
    repo: Path,
    ref: str | None,
    patch_path: Path | None,
    commands: list[str],
    config: SupervisorConfig,
    profile_path: Path | None,
    env: dict[str, str],
    changed_files: tuple[str, ...] | None = None,
    timeout_seconds: float | None = None,
    uv_cache_dir: Path | None = None,
    evidence_dir: Path | None = None,
    loopback_ports: tuple[int, ...] = (),
    node_cache_dir: Path | None = None,
    count_pytest: bool = False,
    baseline_counts: dict[str, int] | None = None,
    read_allowlist: tuple[Path, ...] | None = None,
) -> ReverifyOutcome:
    """Apply the patch to a clean worktree and re-run the verification command(s).

    ``timeout_seconds`` bounds each re-run command; the 300s default predates
    real pinned suites (skep's own takes ~10min serially — the dogfood run
    019fc72c timed out at -1 while perfectly healthy). Callers with the run's
    budget in hand pass it so the re-run is afforded what the worker was.

    ``uv_cache_dir`` (v109-F4) is a per-project cache; when set, priming
    resolves into it instead of a worktree-local throwaway, so the work
    survives across runs. None keeps the throwaway. It must be a cache no
    worker can write (``reverify_run`` passes re-verify's own half of the
    project root): uv installs cached files without re-checking them.

    ``loopback_ports`` (D6, I2): the run's loopback ports (the project's and
    its local model provider's, ES-F252), so a verify that talks to a local
    service runs as the worker's did.

    ``node_cache_dir`` (v127-A0) is the uv cache's twin for a Node prime; None
    keeps a worktree-local throwaway.

    ``count_pytest`` (V1): the single command is a pytest pin, so its counts
    are read on the baseline (``baseline_counts`` when the caller has them
    cached, else by running it right before the patch applies) and on the
    patched tree, into ``test_counts``.

    ``read_allowlist`` (ADR 0068): the run's read wall, the same one its
    worker had (``sandbox.read_allowlist_for``) — every prime and the verify
    run inside it; None keeps reads denylist-only.
    """
    if patch_path is None or not patch_path.is_file():
        # v65-F1: 58 of the first 94 reverifications were patch-less runs
        # (script/researcher by design, no-change audits and reviews) rendered
        # in the same shape a lying worker would earn. The worker's own
        # changed_files claim splits the honest cases from the suspicious one.
        if changed_files is not None and len(changed_files) == 0:
            return ReverifyOutcome(
                "not_applicable", commands, [], "run changed no files — no patch to re-verify"
            )
        if changed_files:
            return ReverifyOutcome(
                "unavailable",
                commands,
                [],
                f"worker claimed {len(changed_files)} changed file(s) "
                "but deposited no patch artifact",
            )
        # v81-F5: changed_files unrecorded AND no patch — a genuinely patch-less
        # run (crash before results, script/researcher lanes), not a suspicious
        # one. "unavailable" here rendered as NOT CONFIRMED on every surface —
        # alarm noise, not honesty (I8).
        return ReverifyOutcome(
            "not_applicable", commands, [], "no patch was produced — nothing to re-verify"
        )
    if not commands:
        return ReverifyOutcome("unavailable", [], [], "worker recorded no verification command")

    # v89-F1: the other creator. run_task shields the ``reverify-<task_id>``
    # name in _ACTIVE long before this runs, so the window here is narrow — but
    # the recovery path re-verifies without that shield, and a creator that
    # skips the lock is the shape of the bug, not a special case.
    with TREE_LOCK:
        worktree = create_worktree(repo, config.worktrees_root, f"reverify-{patch_path.stem}", ref)
    # Council round 3 (N4): one stack holds every cleanup — proxies, bridge
    # socket dirs, the worktree — and runs each even when another raises.
    stack = contextlib.ExitStack()
    stack.callback(remove_worktree, repo, worktree)
    try:
        env = dict(env)
        # v107-F3: TMPDIR inside the wall — unset, tools fall back to /tmp,
        # and any nested bwrap the verify command spawns (skep's own suite)
        # tmpfs-masks it. Same fix as the worker's toolchain env.
        reverify_tmp = worktree / ".reverify-tmp"
        reverify_tmp.mkdir(parents=True, exist_ok=True)
        env.update(sandbox.in_wall_tmp_env(reverify_tmp))  # v120-F1: + TMPPREFIX
        env["CLANG_MODULE_CACHE_PATH"] = str(reverify_tmp / "clang")  # ignores TMPDIR
        # v109-F4: a shared cache sits outside the worktree, so the prime
        # profile needs it as an explicit writable root. The offline verify
        # never writes either cache: patch code runs there, and a never-landed
        # patch must not leave anything a later prime installs from — uv
        # installs cached files without re-checking them (council rounds 2
        # and 3, I2). It reads the Node cache and gets a throwaway uv cache
        # (uv takes a lock in its cache, so a read-only one will not run).
        node_cache = node_cache_dir or worktree / ".node-cache"
        cache_writable = [d for d in (uv_cache_dir, node_cache_dir) if d is not None]
        verify_readable = [node_cache_dir] if node_cache_dir is not None else []
        primed = ""
        node_pin = _pin_uses(commands, _NODE_TOOLS)
        node_prime = _node_prime(worktree, commands)
        if isinstance(node_prime, str):
            if _pin_uses(commands, _NODE_PACKAGE_MANAGERS):
                return ReverifyOutcome(
                    "unavailable",
                    commands,
                    [],
                    f"not confirmed: {node_prime}; the verification command was not run",
                )
            # A bare `node` pin may import nothing it lacks: it runs, unprimed,
            # and the record says so (never unavailable forever).
            primed = f"; ran unprimed: {node_prime}"
        uv_prime = _needs_uv_priming(worktree, commands)
        # The BASELINE decides who the prime may reach (read before the
        # patch applies): the default indexes plus the hosts its lockfiles
        # already resolve from, and the installer files a patch must not touch.
        hosts: set[str] = set()
        if uv_prime:
            uv_index = r'(?:registry|url|git) = "([^"]+)"'
            hosts |= {*_PYPI_HOSTS, *_lock_hosts(worktree, ["uv.lock"], uv_index)}
        if node_pin:
            node_lockfiles = [lockfile for lockfile, _ in _NODE_INSTALLS]
            hosts |= {*_NPM_HOSTS, *_lock_hosts(worktree, node_lockfiles, f"({_TARBALL_URL})")}
            env.update(_node_env(node_cache, reverify_tmp))
        package_manager = _manifest(worktree).get("packageManager")
        protected: set[str] = set()
        for rc in (".yarnrc", ".yarnrc.yml"):
            with contextlib.suppress(OSError):
                text = (worktree / rc).read_text(encoding="utf-8")
                protected |= {str(PurePosixPath(p)) for p in _YARN_PATH.findall(text)}
        wall = _PrimeWall(
            stack,
            tuple(sorted(hosts)),
            worktree=worktree,
            profile_path=profile_path,
            writable=cache_writable,
            config=config,
            read_allowlist=read_allowlist,
        )
        # v127 council round 4 (N1, second door): an install AFTER the patch
        # applies builds/installs patch-chosen code (a uv build backend, a
        # lockfile's packages), so it gets its own wall with no shared cache
        # writable and throwaway caches in the re-verify tmp dir — it can
        # never poison the per-project cache every later baseline prime
        # trusts. The cost is a cold download, only for dependency changes.
        patched_wall = _PrimeWall(
            stack,
            tuple(sorted(hosts)),
            worktree=worktree,
            profile_path=profile_path,
            writable=[],
            config=config,
            name="patched-prime",
            read_allowlist=read_allowlist,
        )
        if uv_prime:
            # v94-F7: prime the BASELINE env before the patch is applied — only
            # code the operator's repo already contains at HEAD runs with the
            # (index-only) network; patch code stays offline under the deny-all
            # profile below, and the operator's own ~/.cache/uv is never written.
            # v109-F4: the cache is the per-project one dispatch primes too,
            # when the caller resolved it — a workspace-local cache dies with
            # the worktree, which made every re-verify resolve from zero.
            cache_dir = uv_cache_dir if uv_cache_dir is not None else worktree / ".uv-cache"
            # Copies, never hard links (uv's Linux default): a venv file linked
            # to the cache would let the verify step write the cache through it.
            uv_prime_env = {**env, "UV_CACHE_DIR": str(cache_dir), "UV_LINK_MODE": "copy"}
            prime_code, _prime_tail = wall.run("uv sync", uv_prime_env)
            if prime_code != 0:
                # A prime that cannot complete is a supervisor-side environment
                # problem, not patch guilt — the honest shape is "unavailable"
                # (the exit-127 rule), never a failed patch.
                return ReverifyOutcome(
                    "unavailable",
                    commands,
                    [prime_code],
                    "could not prime the baseline uv environment for offline "
                    f"re-verification (uv sync exited {prime_code}); the "
                    "verification command was not run",
                )
            env["UV_CACHE_DIR"] = str(reverify_tmp / "uv-cache")
            env["UV_NO_SYNC"] = env["UV_OFFLINE"] = "1"
            primed = f"{primed}; deps primed from the baseline (uv sync), verify ran offline"
        if isinstance(node_prime, tuple):
            # v127-A0: the same shape for Node — the BASELINE lockfile installs
            # under the prime wall; patch code stays offline below.
            node_install = node_prime[1]
            prime_code, prime_output = wall.run(node_install, env)
            if prime_code != 0:
                missing = _corepack_missing(node_install, prime_output, package_manager)
                return ReverifyOutcome(
                    "unavailable",
                    commands,
                    [prime_code],
                    "could not prime the baseline Node dependencies for offline "
                    f"re-verification ({node_install} exited {prime_code})"
                    f"{': ' + missing if missing else ''}; the "
                    "verification command was not run\n--- install output (tail) ---\n"
                    f"{_output_tail(prime_output) or '(no output)'}",
                )
            primed = (
                f"{primed}; node deps primed from the baseline ({node_install}), verify ran offline"
            )
        network = sandbox.DENY_ALL_NETWORK
        proxy: FilteringProxy | None = None
        ports = run_loopback_ports(loopback_ports, (), config.home)
        if profile_path is not None and ports and sandbox.availability().backend == "bubblewrap":
            # D6 + I2: bubblewrap's deny-all netns has no host loopback, so the
            # project's ports come in over the netshim bridge, through a proxy
            # that admits those ports alone (empty allowlist: still offline).
            proxy = _start_proxy(stack, (), config=config, bridge=True, loopback_ports=ports)
            network = sandbox.NetworkPolicy(allow=("localhost",))
        if profile_path is not None:
            try:
                sandbox.write_profile(
                    profile_path,
                    workspace=worktree,
                    extra_writable=git_metadata_writable_roots(worktree),
                    extra_readable=[*git_metadata_readable_roots(worktree), *verify_readable],
                    network=network,
                    proxy_port=proxy.port if proxy is not None else None,
                    unix_socket_path=proxy.unix_socket_path if proxy is not None else None,
                    skep_home=config.home.parent,
                    loopback_ports=ports,
                    read_allowlist=read_allowlist,
                )
            except sandbox.SandboxUnavailableError as exc:
                # ES-F251 (ES-F1, I12): no wall, no re-run — honestly
                # unavailable with the backend's reason, never patch guilt.
                return ReverifyOutcome(
                    "unavailable",
                    commands,
                    [],
                    f"the re-verify sandbox could not be built, so the verification "
                    f"command was not run: {exc}",
                )
        baseline_source: str | None = "cache" if baseline_counts is not None else None
        if count_pytest and baseline_counts is None:
            # V1: the pin on the BASELINE, before the patch applies — counted
            # on exit 1 too (an already-failing suite still has a total).
            # No bytecode: a same-size edit in the same second would run stale.
            # Its own TMPDIR, removed after (review R1, F1).
            baseline_tmp = reverify_tmp / "baseline"
            baseline_tmp.mkdir()
            # Review R2 (Opus): what the prime already put in the tree (an
            # in-place editable build, a yarn PnP map) stays; only what the
            # baseline run adds is removed afterwards.
            pre_baseline = _untracked(worktree)
            code, _output, stdout = _run_command(
                commands[0],
                cwd=worktree,
                env={
                    **env,
                    **sandbox.in_wall_tmp_env(baseline_tmp),
                    "PYTHONDONTWRITEBYTECODE": "1",
                },
                profile_path=profile_path,
                timeout=command_timeout_seconds(timeout_seconds),
            )
            baseline_counts = pytest_counts(stdout) if code in _COUNTABLE_EXITS else None
            baseline_source = "run" if baseline_counts is not None else None
            # What the baseline run wrote must not reach the patched run (I2,
            # review R1 F1): tracked edits reverted and every untracked or
            # ignored path it added removed (review R2: only what it added).
            shutil.rmtree(baseline_tmp, ignore_errors=True)
            reset = subprocess.run(
                ["git", "-C", str(worktree), "reset", "-q", "--hard", "HEAD"], check=False
            )
            for path in _untracked(worktree) - pre_baseline:
                with contextlib.suppress(OSError):  # what stays is caught below
                    (worktree / path).unlink()
            leftover = baseline_tmp.exists() or _untracked(worktree) - pre_baseline
            if reset.returncode or leftover:
                # Review R2 (Terra): a tree git could not reset is not a clean
                # baseline for the patched run — confirm nothing (I2).
                return ReverifyOutcome(
                    "unavailable",
                    commands,
                    [],
                    "the baseline test run left files git could not clean up, so the "
                    "patched run would not start from a clean tree; not re-verified",
                )
        applied = subprocess.run(
            ["git", "-C", str(worktree), "apply", str(patch_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        if applied.returncode != 0:
            return ReverifyOutcome(
                "failed",
                commands,
                [],
                f"patch did not apply to a clean worktree at {ref or 'HEAD'}: "
                f"{applied.stderr.strip()}",
            )
        touched = set(patch_paths(worktree, patch_path)) if uv_prime or node_pin else set()
        if uv_prime and touched & _UV_PRIME_INPUTS:
            # v115-F13 (I2): the baseline prime installed the OLD dependencies;
            # a patch that changes the lockfile must be verified against the
            # NEW ones. Run 01a16157 bumped starlette 1.3.0 → 1.9.0 in uv.lock
            # and re-verification ran the suite on 1.3.0 — a breaking bump would
            # have passed. Prime again, after the patch, through the baseline's
            # index hosts alone and a throwaway cache (patched_wall above); the
            # pinned command still runs offline afterwards.
            patched_env = {**uv_prime_env, "UV_CACHE_DIR": str(reverify_tmp / "uv-patched")}
            if node_pin:  # its XDG/npm caches point at the shared Node cache too
                patched_env.update(_node_env(reverify_tmp / "node-patched", reverify_tmp))
            resync_code, resync_output = patched_wall.run("uv sync", patched_env)
            if resync_code != 0:
                return ReverifyOutcome(
                    "unavailable",
                    commands,
                    [resync_code],
                    "the patch changes the lockfile and its packages could not be "
                    f"primed (uv sync exited {resync_code}); the verification command "
                    "was not run — a dependency change is never confirmed against the "
                    "old dependencies\n--- uv sync output (tail) ---\n"
                    f"{_output_tail(resync_output) or '(no output)'}",
                )
            primed = f"{primed}; re-primed against the patched lockfile"
        patched_prime = _node_prime(worktree, commands) if touched & _NODE_PRIME_INPUTS else None
        if isinstance(patched_prime, str) and not isinstance(node_prime, str):
            # The patch takes the lockfile away, or adds the first dependencies
            # without one: nothing to install them from, so no verdict either
            # way — never confirmed against the old packages, never "failed".
            return ReverifyOutcome(
                "unavailable",
                commands,
                [],
                f"not confirmed: the patch changes the Node dependencies and leaves "
                f"{patched_prime}; the verification command was not run",
            )
        if isinstance(patched_prime, tuple):
            # v127-A0, the v115-F13 twin: a lockfile change (or the first
            # lockfile) is verified against the NEW packages. That install is
            # patch data reaching the network before approval: through the
            # baseline's registry hosts alone, scripts off, and only when the
            # installer itself is unchanged (I12, I11).
            _lockfile, install = patched_prime
            refusal = _node_install_refusal(
                worktree, touched, package_manager=package_manager, protected=protected
            )
            if refusal:
                return ReverifyOutcome(
                    "unavailable",
                    commands,
                    [],
                    "the patch changes the Node dependencies and was not installed: "
                    f"{refusal}; the verification command was not run — a dependency "
                    "change is never confirmed against the old dependencies",
                )
            reprime_code, reprime_output = patched_wall.run(
                install, {**env, **_node_env(reverify_tmp / "node-patched", reverify_tmp)}
            )
            if reprime_code != 0:
                missing = _corepack_missing(install, reprime_output, package_manager)
                return ReverifyOutcome(
                    "unavailable",
                    commands,
                    [reprime_code],
                    "the patch changes the lockfile and its packages could not be "
                    f"primed ({install} exited {reprime_code})"
                    f"{': ' + missing if missing else ''}; the verification "
                    "command was not run — a dependency change is never confirmed "
                    "against the old dependencies\n--- install output (tail) ---\n"
                    f"{_output_tail(reprime_output) or '(no output)'}",
                )
            again = "re-" if isinstance(node_prime, tuple) else ""
            primed = f"{primed}; {again}primed against the patched lockfile ({install})"
        exit_codes: list[int] = []
        outputs: list[str] = []
        stdouts: list[str] = []
        command_timeout = command_timeout_seconds(timeout_seconds)
        for command in commands:
            code, output, stdout = _run_command(
                command,
                cwd=worktree,
                env=env,
                profile_path=profile_path,
                timeout=command_timeout,
            )
            exit_codes.append(code)
            outputs.append(output)
            stdouts.append(stdout)
        if any(code == _COMMAND_NOT_FOUND for code in exit_codes):
            return ReverifyOutcome(
                "unavailable",
                commands,
                exit_codes,
                "verification command not found on the supervisor (toolchain mismatch)",
            )
        test_counts: dict[str, Any] | None = None
        if count_pytest:
            patched = pytest_counts(stdouts[0]) if exit_codes[0] in _COUNTABLE_EXITS else None
            usable = baseline_counts is not None and patched is not None
            test_counts = {
                "status": "ok" if usable else "unparseable",
                "runner": "pytest",
                "baseline": baseline_counts,
                "patched": patched,
                "baseline_source": baseline_source,
            }
        if all(code == 0 for code in exit_codes):
            return ReverifyOutcome(
                "passed",
                commands,
                exit_codes,
                f"re-ran clean: all exit 0{primed}",
                test_counts=test_counts,
            )
        # v115-F12: the failed re-run's own words ride the record; the full
        # output lands beside the profile in the audit dir when there is one.
        evidence = ""
        if evidence_dir is not None:
            evidence_dir.mkdir(parents=True, exist_ok=True)
            output_path = evidence_dir / "reverify.output.txt"
            output_path.write_text(
                "\n\n".join(
                    f"$ {command}\n(exit {code})\n{output}"
                    for command, code, output in zip(commands, exit_codes, outputs, strict=True)
                ),
                encoding="utf-8",
            )
            evidence = f"; full output: {output_path}"
        failed = next((o for c, o in zip(exit_codes, outputs, strict=True) if c != 0), "")
        failed_tail = _output_tail(failed)
        return ReverifyOutcome(
            "failed",
            commands,
            exit_codes,
            f"re-run exit codes {exit_codes} (expected all 0){primed}{evidence}\n"
            f"--- re-run output (tail) ---\n{failed_tail or '(no output)'}",
            test_counts=test_counts,
        )
    except _PrimeUnavailable as exc:
        # N3: a prime that cannot start is a supervisor-side problem — the
        # same honest "unavailable" as a failed install, never a crash.
        return ReverifyOutcome(
            "unavailable",
            commands,
            [],
            f"could not prime: the prime's proxy or sandbox could not be started ({exc}); "
            "the verification command was not run",
        )
    finally:
        stack.close()


def _recorded_read_wall(store: RunStore, task_id: str) -> tuple[Path, ...] | None:
    """The wall a run's ``dispatched`` line recorded (ADR 0068): its worker's
    ``read_allowlist``, or (G3) ``g10_read_allowlist`` for a worker that had
    no wall; None for a run that read denylist-only."""
    detail = next((d for st, d, _ in store.transitions_for(task_id) if st == "dispatched"), None)
    fields = json.loads(detail or "{}")
    recorded = fields.get("read_allowlist", fields.get("g10_read_allowlist"))
    return None if recorded is None else tuple(Path(path) for path in recorded)


def _moved_read_roots(paths: tuple[Path, ...]) -> list[Path]:
    """G1: recorded targets that are a link now, or no longer resolve to
    themselves (a parent swapped for a link) — binding one would read
    whatever it points at today, not what the worker read."""
    return [path for path in paths if path.is_symlink() or os.path.realpath(path) != str(path)]


def reverify_run(
    *,
    store: RunStore,
    task_id: str,
    worker_outcome: str | None,
    repo: Path,
    ref: str | None,
    config: SupervisorConfig,
    changed_files: tuple[str, ...] | None = None,
    verify_command: str = "",
    timeout_seconds: float | None = None,
    loopback_ports: tuple[int, ...] = (),
) -> ReverifyOutcome:
    """Re-verify one completed run from its stored evidence; record the result.

    ``verify_command`` is the project's pinned command (v88-F4). When set it is
    what gets re-run; when empty the supervisor falls back to the command the
    worker nominated in its verify.result event — the pre-v88 behaviour, kept
    for unpinned projects and every non-project run.
    """
    artifacts = {kind: Path(path) for kind, path, _ in store.artifacts_for(task_id)}
    patch_path = artifacts.get("patch")
    pinned = verify_command.strip()
    # I2: the worker's nomination is a claim like any other. A project that has
    # said what verification means outranks it.
    commands = [pinned] if pinned else verification_commands_from_events(store.events_for(task_id))
    commands_source = "project" if pinned else "worker"

    def unavailable(detail: str) -> ReverifyOutcome:
        outcome = ReverifyOutcome(
            "unavailable", commands, [], detail, commands_source=commands_source
        )
        store.record_reverification(
            task_id,
            outcome=outcome.outcome,
            worker_outcome=worker_outcome,
            confirmed=False,
            commands=outcome.commands,
            exit_codes=outcome.exit_codes,
            detail=outcome.detail,
        )
        return outcome

    profile_path: Path | None = None
    if config.sandbox:
        if not sandbox.available():
            # ES-F1 (B47, I12): the patch is the worker's code; running its
            # verify on the bare host is exactly what the wall is for.
            return unavailable(
                f"sandbox unavailable ({sandbox.availability().reason}) — the patch's "
                "verify command was not run unconfined on the host; fix the backend "
                "(`skep doctor`) and re-verify"
            )
        profile_path = config.audit_dir / task_id / "reverify.profile.sb"

    # v109-F4: prime against a per-project cache, so the resolve work one
    # re-verify pays for, the next one keeps. (Runtime import: the resolver
    # pulls in the serve tree — the same cycle dispatch dodges.)
    from .policy_resolver import project_cache_root

    # ADR 0068 (W1): the wall the worker ran in, as its dispatched line
    # recorded it — never re-resolved, so a policy edit since (or a project
    # the repo lookup cannot find) never widens G10's reads past the worker's.
    read_allowlist = _recorded_read_wall(store, task_id) if profile_path is not None else None
    if moved := _moved_read_roots(read_allowlist or ()):
        # G1 (I12): G10 binds exactly the targets dispatch resolved, or nothing.
        return unavailable(
            "extra_read_paths: the run's recorded read wall no longer resolves to itself: "
            + ", ".join(f"{path} now leads to {os.path.realpath(path)}" for path in moved)
            + " — binding it would read past the wall the worker had, so the "
            "verification command was not run; re-dispatch the run to re-verify it"
        )

    # v127-A0 review (council round 3, I2): re-verify's own half of the
    # project root — the worker's sandbox mounts only ``worker/``, so nothing
    # a worker writes reaches what a re-verify installs from.
    uv_cache_dir = project_cache_root(store, config, repo) / "reverify" / "uv"
    uv_cache_dir.mkdir(parents=True, exist_ok=True)
    node_cache_dir = uv_cache_dir.parent / "node"  # v127-A0: the same root, Node's half
    node_cache_dir.mkdir(exist_ok=True)

    # ES-F3 (B131, I2): pin the commit BEFORE re-verifying, and verify on that
    # commit — a name like `main` can move between this run and the landing,
    # and the landing must build on exactly the tree that was confirmed.
    base_commit = resolve_commit(repo, f"{_resolve_ref(repo, ref) if ref else 'HEAD'}^{{commit}}")

    # V1: count the project's own pytest pin; cached per (repo, base, pin).
    count_pytest = bool(pinned) and _is_pytest_pin(pinned)
    cached = (
        store.cached_test_counts(str(repo), base_commit, pinned)
        if count_pytest and base_commit
        else None
    )
    outcome = reverify(
        repo=repo,
        ref=base_commit or ref,
        patch_path=patch_path,
        commands=commands,
        config=config,
        profile_path=profile_path,
        evidence_dir=config.audit_dir / task_id,
        env={name: os.environ[name] for name in ("PATH", "HOME") if name in os.environ},
        changed_files=changed_files,
        timeout_seconds=timeout_seconds,
        uv_cache_dir=uv_cache_dir,
        loopback_ports=loopback_ports,
        node_cache_dir=node_cache_dir,
        count_pytest=count_pytest,
        baseline_counts=cached,
        read_allowlist=read_allowlist,
    )
    counts = outcome.test_counts
    if counts is not None and counts["baseline_source"] == "run" and base_commit:
        store.cache_test_counts(str(repo), base_commit, pinned, counts["baseline"])
    if counts is None and outcome.exit_codes and not count_pytest:
        # I8: a runner whose counts V1 cannot read is said so, never guessed.
        outcome = replace(outcome, test_counts={"status": "unknown", "runner": None})
    # I8: the record says WHAT it re-ran, not just how it went — "passed" means
    # something different when the worker chose the command than when the
    # project did. Only stamped when commands actually ran.
    if outcome.exit_codes:
        source_note = (
            "the project's pinned verify_command"
            if commands_source == "project"
            else "the worker's own verify step"
        )
        outcome = replace(
            outcome,
            commands_source=commands_source,
            detail=f"{outcome.detail} [command from {source_note}]",
        )
    else:
        outcome = replace(outcome, commands_source=commands_source)
    outcome = _v1_verdict(outcome, repo, patch_path, base_commit or ref or "HEAD")
    confirmed = worker_outcome == "passed" and outcome.outcome == "passed"
    store.record_reverification(
        task_id,
        outcome=outcome.outcome,
        worker_outcome=worker_outcome,
        confirmed=confirmed,
        commands=outcome.commands,
        exit_codes=outcome.exit_codes,
        detail=outcome.detail,
        base_commit=base_commit if outcome.exit_codes else None,
        tamper_flags=outcome.tamper_flags,
        test_counts=outcome.test_counts,
    )
    return outcome


def _v1_verdict(
    outcome: ReverifyOutcome, repo: Path, patch_path: Path | None, base: str
) -> ReverifyOutcome:
    """V1: a patch that changed the tests under the check is "tampered" —
    whatever the re-run said, it did not confirm the patch."""
    flags: list[str] = []
    if patch_path is not None and patch_path.is_file() and outcome.outcome != "not_applicable":
        flags = static_tamper_flags(repo, patch_path, base)
    counts = outcome.test_counts or {}
    if counts.get("status") == "ok" and outcome.outcome == "passed":
        # Review R1 (F3): counts only speak for a clean re-run — a `-x` pin
        # stops early on an honest regression, which stays "failed".
        flags += count_flags(counts["baseline"], counts["patched"])
    if flags:
        totals = (
            f" (executed tests {counts['baseline']['total_executed']} on the baseline, "
            f"{counts['patched']['total_executed']} patched)"
            if counts.get("status") == "ok"
            else ""
        )
        return replace(
            outcome,
            outcome="tampered",
            tamper_flags=tuple(flags),
            detail=f"test tampering: {', '.join(flags)}{totals}. {V1_SCOPE}. "
            f"The re-run itself: {outcome.outcome}: {outcome.detail}",
        )
    if counts.get("status") == "unparseable":
        return replace(
            outcome,
            detail=f"{outcome.detail} [test counts unparseable: auto-landing is blocked]",
        )
    return outcome
