"""D3: auto-approval policy rules — Queen-side autonomy on patch-as-approval.

Declarative rules that auto-apply a worker's patch when conditions hold:
verification passed, supervisor re-verification confirmed it (G10), no risk
flags, and the diff stays within an allowed scope. This sits entirely Queen-side
on top of Q5's patch-as-approval (applying the patch *is* the approval) — zero
contract change.

The mechanism is built and tested in v2 but **dormant by default**: with no
rules configured, nothing is auto-approved and the human loop is unchanged. It
goes active in v3 (the first recurring workload, U1). Every auto-approval is
recorded in the approval queue with the rule that fired (`resolved_by =
auto:<rule>`), so the audit trail always names what granted the autonomy.
"""

from __future__ import annotations

import configparser
import json
import os
import re
import shlex
import subprocess
import tempfile
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path

from .apply import apply_patch_on_branch, moved_past_verified_base, patch_paths
from .shell_prefixes import launched_command
from .store import RunStore


@dataclass(frozen=True)
class AutoApprovalRule:
    """One declarative grant of autonomy. All enabled conditions must hold."""

    name: str
    require_verification_passed: bool = True
    require_reverified: bool = True
    forbid_risk_flags: bool = True
    # fnmatch globs; every changed file must match at least one. Default allows
    # anything — narrow it (e.g. lockfiles only) for an unattended workload.
    diff_scope: tuple[str, ...] = ("*",)
    max_changed_files: int | None = None
    # v90-F4: require that the re-verification was earned by the PROJECT's
    # pinned verify_command, not the command the worker nominated for itself
    # (v88-F4). v127-F5 round 3 (I2): on by default, so a library caller's
    # own rule cannot skip the pin by leaving the field out.
    require_pinned_verify: bool = True


# Manifest / lockfile globs — the "safe" diff scope U1 auto-lands (D3 active).
MANIFEST_DIFF_SCOPE: tuple[str, ...] = (
    "requirements.txt",
    "requirements*.txt",
    "constraints.txt",
    "uv.lock",
    "poetry.lock",
    "Pipfile.lock",
    "package-lock.json",
    "yarn.lock",
    "*.lock",
)

# The built-in rule that makes D3 active for U1: auto-apply a patch only when the
# worker verified it, the supervisor re-verified it (G10), there are no risk flags
# (e.g. a major-version bump files for review instead), and the diff touches only
# manifest/lockfiles. v127-F5 round 3: library callers only — `--auto-approve`
# no longer installs it (it could land only where a pinned maintain-phase
# project's verified-patch rule lands anyway).
# v127-F5 round 2 (I2): it needs the PROJECT's pin like every auto-landing rule
# — a lockfile patch the worker "verified" with its own choice of command landed.
SAFE_DEPENDENCY_RULE = AutoApprovalRule(
    name="deps-safe",
    require_verification_passed=True,
    require_reverified=True,
    forbid_risk_flags=True,
    diff_scope=MANIFEST_DIFF_SCOPE,
    max_changed_files=10,
    require_pinned_verify=True,
)

# The interactive serve/chat policy toggle means "land verified worker patches"
# for local supervised coding runs. It still requires supervisor re-verification
# and no worker risk flags, but it is not limited to dependency manifests.
# v90-F4: this is the rule the maintain phase and the serve/chat "land verified
# patches" toggle both resolve to — the only lane that lands broad diffs without
# a human. A worker-nominated verify command (`true` earns a confirmed
# re-verification for a broken patch) must not satisfy it.
VERIFIED_PATCH_RULE = AutoApprovalRule(name="verified-patch", require_pinned_verify=True)


# v127-F5: commands that pass whatever the patch says. A list of shapes seen in
# the field (store's `git diff --check`, a worker's `true`, npm's placeholder
# `echo "Error: no test specified"`), not a proof — so the word is
# "known-vacuous", never "cannot fail". G10 runs the pin through `/bin/sh -c`,
# so the predicate reads it the way the shell will — a rule, not a parser
# (round 3): the exit status is the LAST list's after `;`/newline; in an
# and-or list `&&` needs every side vacuous and `||` any side; a pipe, a
# background `&` or a command substitution can hide the code anywhere.
_VACUOUS_ARGV0 = frozenset(
    {"true", ":", "test", "[", "echo", "printf", "ls", "cat", "cd", "pushd", "popd"}
)
_SHELLS = frozenset({"sh", "bash", "zsh", "dash"})
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
_MASKING_TOKENS = frozenset({"|", "&", "|&", ";;", ";&"})
_GIT_OPTS_WITH_VALUE = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace"})
# shlex glues a run of punctuation into one token (`;\n`, `&&\n`); split it back
# into sh operators, longest first.
_OPERATOR = re.compile(r"&&|\|\||\|&|;;|;&|&>|>&|<&|>>|<<|<>|>\||[;&|\n()<>]")


def _tokens(command: str) -> list[str] | None:
    lexer = shlex.shlex(command, posix=True, punctuation_chars="();<>|&\n")
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    try:
        raw = list(lexer)
    except ValueError:
        return None
    tokens: list[str] = []
    for token in raw:
        ops = _OPERATOR.findall(token)
        tokens.extend(ops if ops and "".join(ops) == token else [token])
    return tokens


def _segment_vacuous(argv: list[str]) -> bool:
    while argv and (os.path.basename(argv[0]) == "env" or _ASSIGNMENT.match(argv[0])):
        argv = argv[1:]
    if not argv:
        return True
    name = os.path.basename(argv[0])
    if name in _SHELLS and len(argv) >= 3 and argv[1] in ("-c", "-lc"):
        return known_vacuous_verify(argv[2]) is not None
    if name == "exit":
        return argv[1:] in ([], ["0"])
    if name == "git":
        i = 1
        while i < len(argv) and argv[i].startswith("-"):
            i += 2 if argv[i] in _GIT_OPTS_WITH_VALUE else 1
        sub, rest = argv[i : i + 1], argv[i + 1 :]
        if sub == ["diff"]:
            return {r for r in rest if r.startswith("-") and r != "--"} <= {"--check"}
        return sub in (["status"], ["log"])
    return name in _VACUOUS_ARGV0


def _and_or_vacuous(
    tokens: list[str], weak: Callable[[list[str]], bool] = _segment_vacuous
) -> bool:
    """sh's and-or list, left to right at equal precedence: ``a && b`` always
    passes iff both do, ``a || b`` iff either does (an empty side counts).
    ``weak`` judges one segment (v133-F4: also syntax-only)."""
    vacuous: bool | None = None
    joiner, segment = "&&", []
    for token in [*tokens, "&&"]:
        if token not in ("&&", "||"):
            segment.append(token)
            continue
        this = weak(segment)
        if vacuous is None:
            vacuous = this
        else:
            vacuous = (vacuous and this) if joiner == "&&" else (vacuous or this)
        joiner, segment = token, []
    return bool(vacuous)


# Round 3: what an operator reads names no internals (known-vacuous, G10).
PIN_HINT = (
    'Pin the command that runs your tests: skep project setup <repo> --verify-command "<command>"'
)


def cannot_land_on(reason: str) -> str:
    """``reason`` (a known-vacuous verdict) as the refusal an operator reads."""
    return f"{reason}, so skep can't land a patch on it by itself. {PIN_HINT}"


def external_engine_pin_refusal(engine_name: str, verify_command: str) -> str | None:
    """v90-F1 / v127-F5 (I2): why an external coding agent may not run on this
    pin, or None. Its own check is ``git diff --check`` — whitespace — so the
    project must pin a real one. v132-F6: one sentence for the resolver and
    for run_task's funnel, which every face reaches."""
    vacuous = known_vacuous_verify(verify_command)
    if vacuous is None:
        return None
    why = f" Your pin is no better: {vacuous}." if verify_command.strip() else ""
    return (
        f"coding_engine {engine_name!r} is an external agent whose own "
        "check is `git diff --check` (whitespace only), which passes whether or not the "
        f"patch works, so skep won't run it without a real check.{why} {PIN_HINT} "
        "(chat: setup_project verify_command=...)."
    )


def known_vacuous_verify(command: str) -> str | None:
    """Why ``command`` is a known-vacuous check, in plain words, or None when
    it is not one."""
    shown = command.strip()
    tokens = _tokens(shown)
    if tokens is None:
        return None
    if any(t in _MASKING_TOKENS for t in tokens) or "`" in shown or "$(" in shown:
        return (
            f"`{shown}` can hide a failing exit code, so it passes whether or not the patch works"
        )
    while tokens and tokens[-1] in (";", "\n"):
        tokens.pop()
    cut = max((i + 1 for i, t in enumerate(tokens) if t in (";", "\n")), default=0)
    last = tokens[cut:]
    if not last:
        return "an empty check passes whether or not the patch works"
    if "no test specified" in " ".join(last) or _and_or_vacuous(last):
        return f"`{shown}` passes whether or not the patch works"
    return None


# v133-F4 (F-I1): checks that pass on any patch that parses, or that only ask
# a tool to start — a chat-set `node --check index.js` pin "confirmed" patches
# that touched no file it read. Narrow on purpose: a type check (`tsc
# --noEmit`), a linter or a test runner is a real check. Options before the
# first operand only: `node test.js -c` hands `-c` to the script.
_SYNTAX_FLAGS: dict[str, frozenset[str]] = {
    "node": frozenset({"--check", "-c"}),
    "nodejs": frozenset({"--check", "-c"}),
    **{shell: frozenset({"-n"}) for shell in _SHELLS},
    "ruby": frozenset({"-c"}),
    "perl": frozenset({"-c"}),
    "php": frozenset({"-l"}),
}
_COMPILE_MODULES = frozenset({"py_compile", "compileall"})
_PYTHON = re.compile(r"python[0-9.]*")
_RUNNER_PREFIXES = (("uv", "run"), ("poetry", "run"), ("npx",))


def _segment_syntax_only(argv: list[str]) -> bool:
    while argv and (os.path.basename(argv[0]) == "env" or _ASSIGNMENT.match(argv[0])):
        argv = argv[1:]
    for prefix in _RUNNER_PREFIXES:
        if tuple(argv[: len(prefix)]) == prefix:
            argv = argv[len(prefix) :]
            break
    if not argv:
        return False
    name = os.path.basename(argv[0])
    if name in _SHELLS and len(argv) >= 3 and argv[1] in ("-c", "-lc"):
        return known_vacuous_verify(argv[2]) is not None or syntax_only_verify(argv[2]) is not None
    if argv[1:] in (["--version"], ["--help"]):
        return True
    if _PYTHON.fullmatch(name):
        return argv[1:2] == ["-m"] and argv[2:3] != [] and argv[2] in _COMPILE_MODULES
    options = []
    for token in argv[1:]:
        if not token.startswith("-"):
            break
        options.append(token)
    return bool(_SYNTAX_FLAGS.get(name, frozenset()) & set(options))


def _segment_weak(argv: list[str]) -> bool:
    # v133 R1 (Sol S1): a subshell, assignments and a launcher's own options
    # (`env -C web`, `timeout 60`) only set the check up.
    argv = [token for token in argv if token not in ("(", ")")]
    while argv and _ASSIGNMENT.match(argv[0]):
        argv = argv[1:]
    argv = launched_command(argv)
    return _segment_vacuous(argv) or _segment_syntax_only(argv)


def syntax_only_verify(command: str) -> str | None:
    """v133-F4 (F-I1): why ``command`` is a syntax-only (or version-only)
    check, in plain words, or None. Read like ``known_vacuous_verify`` (the
    last list's and-or); a known-vacuous pin is not also called syntax-only,
    so a gate asks both. Such a pin never qualifies a patch for auto-landing."""
    shown = command.strip()
    if known_vacuous_verify(shown) is not None:
        return None
    tokens = _tokens(shown) or []
    while tokens and tokens[-1] in (";", "\n"):
        tokens.pop()
    cut = max((i + 1 for i, t in enumerate(tokens) if t in (";", "\n")), default=0)
    last = tokens[cut:]
    if not last or not _and_or_vacuous(last, _segment_weak):
        return None
    return (
        f"`{shown}` is syntax-only: it checks that the code parses (or that a tool "
        "starts), not that the patch works"
    )


# v127-F5 round 2 (I2): files that decide what the pinned check runs. G10
# re-runs the pin on the PATCHED tree, so a patch that edits these gets the
# check it wrote. Any touch blocks, except pyproject.toml and package.json,
# whose test sections are compared so dependency bumps still land. Round 3:
# the runner-config names the first list missed (`.pytest.ini` beats
# pytest.ini; `.npmrc` `script-shell=true` makes `npm test` exit 0).
# ponytail: a blocklist of the runners seen in the wild — a runner configured
# by a file not named here is not seen; add its name.
_PIN_CONTROL_GLOBS = (
    "conftest.py",
    "pytest.ini",
    ".pytest.ini",
    "tox.ini",
    "setup.cfg",
    "noxfile.py",
    "Makefile",
    ".coveragerc",
    ".npmrc",
    ".yarnrc*",
    "jest.config.*",
    "vitest.config.*",
    ".mocharc*",
    # round 4: pytest 9's own config files (they win over pytest.ini and
    # pyproject); vitest reads vite.config's `test` block when there is no
    # vitest.config; setup.py is the build script the re-prime runs.
    "pytest.toml",
    ".pytest.toml",
    "vite.config.*",
    "vitest.workspace.*",
    "setup.py",
)
# pyproject [tool.<prefix>*] tables that configure a test runner.
_PYPROJECT_TOOL_PREFIXES = ("pytest", "coverage", "tox", "nox", "poe")
# package.json keys a dependency bump writes; every other key is compared.
# `overrides`/`resolutions` pin transitive versions (npm/yarn) — the same trust
# as `dependencies`, and what dependabot/renovate edit on security bumps.
_PACKAGE_DEPENDENCY_KEYS = frozenset(
    {
        "dependencies",
        "devDependencies",
        "optionalDependencies",
        "peerDependencies",
        "overrides",
        "resolutions",
        "version",
    }
)
_PACKAGE_RUNNERS = frozenset({"npm", "pnpm", "yarn"})
# V1 review R1 (F7): test-runner config proper — what a patch edits to change
# which tests run. The rest of _PIN_CONTROL_GLOBS (vite.config, setup.py,
# Makefile, .npmrc ...) steers the check too, so it blocks auto-landing, but
# editing it is not test tampering and keeps a confirmed G10 (I8).
_TEST_CONFIG_GLOBS = (
    "conftest.py",
    "pytest.ini",
    ".pytest.ini",
    "pytest.toml",
    ".pytest.toml",
    "jest.config.*",
    "vitest.config.*",
)
# Compared section by section (``_test_config(..., runner_only=True)``).
_TEST_CONFIG_PARSED = ("pyproject.toml", "package.json", "setup.cfg", "tox.ini")
# v133-F3 (A3): directories that hold installed dependencies. A manifest in
# one is a dependency's own, not the project's test config (a repo without a
# .gitignore put ~200 node_modules package.json files in one patch, each
# labelled tampering). The files still block auto-landing
# (``vendored_files``): a vendored runner is code the check may load (I2).
_VENDORED_DIRS = frozenset({"node_modules", "vendor", ".venv", "site-packages"})


def vendored_files(changed_files: tuple[str, ...]) -> tuple[str, ...]:
    """The changed files under a vendored dependency directory."""
    return tuple(p for p in changed_files if _VENDORED_DIRS & set(p.split("/")[:-1]))


def _dig(node: object, *keys: str) -> object:
    for key in keys:
        node = node.get(key) if isinstance(node, dict) else None
    return node


def _test_config(name: str, text: str | None, *, runner_only: bool = False) -> object:
    """The parts of ``pyproject.toml``/``package.json`` that steer the check.
    Unparseable → a sentinel that never compares equal (blocks).

    ``runner_only`` (V1 review R1, F7): just the test runner's own section —
    pyproject's ``[tool.pytest*]``, package.json's test scripts and ``jest``
    key, setup.cfg's ``[tool:pytest]``, tox.ini's ``[pytest]`` and its
    ``[testenv*]`` ``commands`` (review R3, H6)."""
    if text is None:
        return None
    if name in ("setup.cfg", "tox.ini"):
        parser = configparser.ConfigParser(interpolation=None)
        try:
            parser.read_string(text)
        except configparser.Error:
            return object()
        section = "tool:pytest" if name == "setup.cfg" else "pytest"
        runner = dict(parser[section]) if parser.has_section(section) else None
        if name == "setup.cfg":
            return runner
        commands = {
            s: parser[s]["commands"]
            for s in parser.sections()
            if (s == "testenv" or s.startswith("testenv:")) and parser.has_option(s, "commands")
        }
        return (runner, commands) if runner is not None or commands else None
    try:
        doc = json.loads(text) if name == "package.json" else tomllib.loads(text)
    except ValueError:
        return object()
    if not isinstance(doc, dict):
        return object()
    if name == "package.json":
        if runner_only:
            scripts = doc.get("scripts")
            scripts = scripts if isinstance(scripts, dict) else {}
            return {k: v for k, v in scripts.items() if "test" in k}, doc.get("jest")
        return {k: v for k, v in doc.items() if k not in _PACKAGE_DEPENDENCY_KEYS}
    tool = doc.get("tool")
    tool = tool if isinstance(tool, dict) else {}
    if runner_only:
        return {k: v for k, v in tool.items() if k.startswith("pytest")}
    return (
        {k: v for k, v in tool.items() if k.startswith(_PYPROJECT_TOOL_PREFIXES)},
        _dig(tool, "hatch", "envs"),
        _dig(tool, "pdm", "scripts"),
        # round 5: every entry point, not just pytest11 — a console script
        # named like the pin's tool (`[project.scripts] pytest = …`) replaces
        # the tool `uv run pytest` runs.
        _dig(doc, "project", "entry-points"),
        _dig(doc, "project", "scripts"),
        _dig(doc, "project", "gui-scripts"),
        # round 4: a patch-chosen build backend runs during the re-prime and,
        # once landed on skep/maintain, during every later baseline prime.
        doc.get("build-system"),
    )


def _git_text(repo: Path, *args: str, env: dict[str, str] | None = None) -> str | None:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        errors="replace",  # a binary blob is unparseable config, never a crash
        env=env,
        check=False,
    )
    return proc.stdout if proc.returncode == 0 else None


def _words(command: str) -> list[str]:
    """Every word of ``command``, and of any quoted word inside it (an
    ``sh -c '...'`` program), so a path cannot hide in a nested string."""
    words = _tokens(command) or []
    return [*words, *(n for w in words if any(c.isspace() for c in w) for n in _words(w))]


def _package_scripts(tokens: list[str], scripts: object) -> list[str]:
    """The package.json scripts a pin runs: ``npm|pnpm|yarn test`` or
    ``... run|run-script <name>``. ponytail: the root package.json, one level
    (a script that runs another script is not followed)."""
    if not isinstance(scripts, dict):
        return []
    found = []
    for i, token in enumerate(tokens):
        if os.path.basename(token) not in _PACKAGE_RUNNERS:
            continue
        rest = tokens[i + 1 : i + 3]
        name = rest[0] if rest[:1] in (["test"], ["t"]) else None
        if rest[:1] in (["run"], ["run-script"]) and len(rest) == 2:
            name = rest[1]
        name = "test" if name == "t" else name
        if isinstance(scripts.get(name), str):
            found.append(scripts[name])
    return found


def pin_control_changes(
    repo: Path,
    patch_path: Path,
    base: str,
    changed_files: tuple[str, ...],
    pin_commands: tuple[str, ...],
) -> tuple[str, ...]:
    """The changed files that steer the pinned check — read by the supervisor
    from the patch artifact applied to ``base`` in a scratch index, never from
    the worker. Every word of the pin (and of the package.json script it runs)
    that names a changed file counts: that file is the check."""
    words = [t for c in pin_commands for t in _words(c)]
    try:
        scripts = _dig(json.loads(_git_text(repo, "show", f"{base}:package.json") or ""), "scripts")
    except ValueError:
        scripts = None
    words += [t for script in _package_scripts(words, scripts) for t in _words(script)]
    pin_paths = {os.path.normpath(t) for t in words if t}
    hits = [
        path
        for path in changed_files
        if any(fnmatch(os.path.basename(path), g) for g in _PIN_CONTROL_GLOBS)
        or os.path.normpath(path) in pin_paths
    ]
    parsed = [p for p in changed_files if os.path.basename(p) in ("pyproject.toml", "package.json")]
    try:
        hits += _config_changes(repo, patch_path, base, parsed, runner_only=False)
    except ConfigUnreadable:  # R4 (N2): a side that is no regular file steers it
        hits += parsed
    return tuple(sorted(set(hits), key=changed_files.index))


class ConfigUnreadable(ValueError):
    """R3 (S12): a runner-config side the diff says exists could not be read
    — never the same as an absent side. ``what`` names the read."""

    def __init__(self, what: str) -> None:
        super().__init__(what)
        self.what = what


_ABSENT_MODE = "000000"  # git's raw-diff mode for a side that does not exist
_FILE_MODES = frozenset({_ABSENT_MODE, "100644", "100755"})


def _mode(repo: Path, treeish: str | None, path: str, env: dict[str, str]) -> str:
    """``path``'s git mode in ``treeish`` (the scratch index when None);
    ``_ABSENT_MODE`` when it is not there."""
    args = ("ls-files", "-s") if treeish is None else ("ls-tree", treeish)
    out = _git_text(repo, *args, "--", path, env=env)
    if out is None:
        raise ConfigUnreadable(f"config_mode:{path}")
    return out.split(maxsplit=1)[0] if out.strip() else _ABSENT_MODE


def test_config_changes(
    repo: Path,
    patch_path: Path,
    base: str,
    changed_files: tuple[str, ...],
    *,
    head: str | None = None,
    modes: dict[str, tuple[str, str]] | None = None,
) -> tuple[str, ...]:
    """V1 (review R1, F7): the changed files that are test-runner config —
    ``_TEST_CONFIG_GLOBS``, ``.github/workflows/**``, and the runner section
    of the ``_TEST_CONFIG_PARSED`` files — read like ``pin_control_changes``.
    ``head`` (Phase 09 R2): read the new side from that commit, not from the
    patch applied to ``base``. ``modes`` (R3, S12): each path's (old, new)
    raw-diff modes — a side they say exists that cannot be read raises
    ``ConfigUnreadable`` instead of comparing as absent."""
    hits = [
        path
        for path in changed_files
        if any(fnmatch(os.path.basename(path), g) for g in _TEST_CONFIG_GLOBS)
        or path.startswith(".github/workflows/")
    ]
    vendored = vendored_files(changed_files)
    parsed = [
        p for p in changed_files if os.path.basename(p) in _TEST_CONFIG_PARSED and p not in vendored
    ]
    hits += _config_changes(
        repo, patch_path, base, parsed, runner_only=True, head=head, modes=modes
    )
    return tuple(sorted(set(hits), key=changed_files.index))


def _config_changes(
    repo: Path,
    patch_path: Path,
    base: str,
    paths: list[str],
    *,
    runner_only: bool,
    head: str | None = None,
    modes: dict[str, tuple[str, str]] | None = None,
) -> list[str]:
    """``paths`` whose ``_test_config`` differs between ``base`` and the patch
    applied in a scratch index; every path when the patch does not apply.

    R4 (N2): a side that is a symlink or a gitlink raises
    ``ConfigUnreadable("config_mode:<path>")`` — ``git show`` of a symlink is
    its target string, and "a=1" parses as config with no runner in it."""
    if not paths:
        return []
    hits = []
    with tempfile.TemporaryDirectory(prefix="skep-pinctl-") as tmp:
        # R5 (Neon): literal pathspecs — `:x/tox.ini` and `?/setup.cfg` are
        # file names here, never magic or a glob the mode probe would misread.
        env = {
            **os.environ,
            "GIT_INDEX_FILE": str(Path(tmp) / "index"),
            "GIT_LITERAL_PATHSPECS": "1",
        }
        applied = head is not None or (
            _git_text(repo, "read-tree", base, env=env) is not None
            and _git_text(
                repo, "apply", "--cached", "--whitespace=nowarn", str(patch_path), env=env
            )
            is not None
        )
        for path in paths:
            name = os.path.basename(path)
            if (
                applied
                and not {
                    _mode(repo, base, path, env),
                    _mode(repo, head, path, env),
                }
                <= _FILE_MODES
            ):
                raise ConfigUnreadable(f"config_mode:{path}")
            before = _git_text(repo, "show", f"{base}:{path}")
            after = _git_text(repo, "show", f"{head or ''}:{path}", env=env)
            old, new = (modes or {}).get(path, (_ABSENT_MODE, _ABSENT_MODE))
            if applied and (
                (before is None and old != _ABSENT_MODE) or (after is None and new != _ABSENT_MODE)
            ):
                raise ConfigUnreadable(f"config_show:{path}")
            if not applied or _test_config(name, before, runner_only=runner_only) != _test_config(
                name, after, runner_only=runner_only
            ):
                hits.append(path)
    return hits


@dataclass(frozen=True)
class AutoApprovalContext:
    verification_outcome: str | None
    reverify_confirmed: bool
    risk_flags: tuple[str, ...] = ()
    changed_files: tuple[str, ...] = ()
    # v90-F4: did the project pin the command G10 re-ran? Known at the dispatch
    # call site (run_task holds verify_command since v88-F4), so nothing has to
    # be persisted or parsed back out of the re-verification detail string.
    verify_pinned: bool = False
    # v127-F5: the commands G10 actually re-ran (reverifications.commands_json).
    reverify_commands: tuple[str, ...] = ()
    # v127-F5 round 2: changed files that steer the pinned check (supervisor-read).
    pin_control_files: tuple[str, ...] = ()
    # V1 (phase-01 1.3): G10's test-tamper flags, and its pytest-count status
    # ("ok" | "unparseable" | "unknown"; None when nothing was counted).
    tamper_flags: tuple[str, ...] = ()
    test_counts_status: str | None = None


@dataclass(frozen=True)
class AutoApproval:
    rule_name: str
    review_id: str
    note: str
    branch: str


def rule_block_reason(rule: AutoApprovalRule, ctx: AutoApprovalContext) -> str | None:
    """Return why ``rule`` does *not* apply, or None when it matches."""
    if not ctx.changed_files:
        return "no changed files to apply"
    # V1 (I2): every rule, whatever its other settings — a check the patch
    # rewrote, or whose counts nobody could read, confirms nothing.
    if ctx.tamper_flags:
        return (
            f"test-tamper guard: the patch changes the tests ({', '.join(ctx.tamper_flags)}), "
            "so a passing check proves nothing and skep can't land it by itself; "
            "the patch is still landable by hand after review"
        )
    if ctx.test_counts_status == "unparseable":
        return (
            "the pinned pytest check's test counts could not be read, so skep can't rule "
            "out dropped tests and won't land the patch by itself; the patch is still "
            "landable by hand"
        )
    if rule.require_verification_passed and ctx.verification_outcome != "passed":
        return f"verification is {ctx.verification_outcome!r}, not 'passed'"
    # Review R1 (F6, I2): every rule — `require_reverified=False` stays for
    # compatibility but no longer lands an unconfirmed G10.
    if not ctx.reverify_confirmed:
        return "supervisor re-verification did not confirm the result"
    if rule.require_pinned_verify and not ctx.verify_pinned:
        # I9: name the missing key and where it goes, not just the refusal.
        return (
            "the project pins no verify_command, so re-verification re-ran the "
            "command the worker chose for itself — set verify_command in the "
            "project policy to auto-land (v88-F4); the patch is still landable "
            "by hand"
        )
    # v127-F5 (I2): every rule, every face — `store` auto-landed on
    # `git diff --check` and `--auto-approve` on a worker's `true`. A landing
    # without a human needs at least one re-run that is not known-vacuous.
    if not any(known_vacuous_verify(c) is None for c in ctx.reverify_commands):
        why = "; ".join(filter(None, (known_vacuous_verify(c) for c in ctx.reverify_commands)))
        if not why:
            return (
                f"no check was re-run, so skep can't land the patch by itself. {PIN_HINT}. "
                "The patch is still landable by hand"
            )
        return f"{cannot_land_on(why)}. The patch is still landable by hand"
    # v133-F4 (F-I1): every re-run that is not vacuous is syntax-only
    if all(known_vacuous_verify(c) or syntax_only_verify(c) for c in ctx.reverify_commands):
        why = "; ".join(filter(None, (syntax_only_verify(c) for c in ctx.reverify_commands)))
        return f"{cannot_land_on(why)}. The patch is still landable by hand"
    vendored = vendored_files(ctx.changed_files)
    if vendored:
        # v133-F3 (A3): not tampering, but code the check may load (I2)
        return (
            f"this patch vendors {len(vendored)} dependency file"
            f"{'' if len(vendored) == 1 else 's'} ({vendored[0]}"
            f"{', ...' if len(vendored) > 1 else ''}), which the check may load, so skep "
            "can't land it by itself; the patch is still landable by hand"
        )
    if ctx.pin_control_files:
        return (
            "the patch changes files that decide what the pinned check runs "
            f"({', '.join(ctx.pin_control_files)}), so skep can't land it by itself; "
            "the patch is still landable by hand"
        )
    if rule.forbid_risk_flags and ctx.risk_flags:
        return f"risk flags present: {list(ctx.risk_flags)}"
    if rule.max_changed_files is not None and len(ctx.changed_files) > rule.max_changed_files:
        return f"{len(ctx.changed_files)} files changed exceeds limit {rule.max_changed_files}"
    out_of_scope = [
        path for path in ctx.changed_files if not any(fnmatch(path, g) for g in rule.diff_scope)
    ]
    if out_of_scope:
        return f"files outside diff scope {list(rule.diff_scope)}: {out_of_scope}"
    return None


def _match_note(rule: AutoApprovalRule, ctx: AutoApprovalContext) -> str:
    return (
        f"rule {rule.name!r} fired: verification={ctx.verification_outcome}, "
        f"re-verified={ctx.reverify_confirmed}, risk_flags={list(ctx.risk_flags)}, "
        f"{len(ctx.changed_files)} file(s) within scope {list(rule.diff_scope)}"
    )


def evaluate(
    rules: tuple[AutoApprovalRule, ...], ctx: AutoApprovalContext
) -> tuple[AutoApprovalRule, str] | None:
    """Return the first matching rule and its audit note, or None if none match."""
    for rule in rules:
        if rule_block_reason(rule, ctx) is None:
            return rule, _match_note(rule, ctx)
    return None


def maybe_auto_approve(
    *,
    store: RunStore,
    rules: tuple[AutoApprovalRule, ...],
    repo: Path,
    task_id: str,
    verification_outcome: str | None,
    risk_flags: tuple[str, ...],
    branch: str | None = None,
    verify_pinned: bool = False,
) -> AutoApproval | None:
    """Evaluate rules for a completed run and, if one fires, apply the patch.

    Returns the recorded auto-approval, or None if no rule matched or there was
    no patch. An apply *failure* after a match is escalated as a pending
    approval (a human picks it up) rather than silently dropped.

    v30: ``branch`` (e.g. ``skep/maintain``) accumulates a project's auto-applied
    patches on ONE integration branch instead of a fresh ``skep/<task_id>`` per
    run — appending a commit when the branch exists (v24-F1). main still never
    advances; the human merges the integration branch. Default keeps the
    per-task branch.
    """
    if not rules:
        return None
    reverify = store.reverification_for(task_id)
    artifacts = {kind: Path(path) for kind, path, _ in store.artifacts_for(task_id)}
    patch_path = artifacts.get("patch")
    # v115-F15: the lane judges the patch it would apply (scope, size, and
    # whether there is anything at all) — the worker's changed_files claim is
    # not an input to a landing decision (I2, I5).
    changed_files = (
        patch_paths(repo, patch_path) if patch_path is not None and patch_path.is_file() else ()
    )
    reverify_commands = tuple(reverify.commands) if reverify is not None else ()
    ctx = AutoApprovalContext(
        verification_outcome=verification_outcome,
        reverify_confirmed=reverify is not None and reverify.confirmed,
        risk_flags=risk_flags,
        changed_files=changed_files,
        verify_pinned=verify_pinned,
        reverify_commands=reverify_commands,
        pin_control_files=(
            pin_control_changes(
                repo,
                patch_path,
                store.verified_base(task_id) or "HEAD",
                changed_files,
                reverify_commands,
            )
            if patch_path is not None and changed_files
            else ()
        ),
        tamper_flags=tuple(reverify.tamper_flags) if reverify is not None else (),
        test_counts_status=(
            (reverify.test_counts or {}).get("status") if reverify is not None else None
        ),
    )
    matched = evaluate(rules, ctx)
    if matched is None:
        # v106-F11 (v90-F4's unkept clause): the reason the lane did NOT fire
        # goes on the run's audit trail. version-history claimed "with the
        # missing key named" for four rounds while the runtime computed the
        # reason and dropped it (I8) — the record now says exactly which
        # requirement each configured rule tripped on.
        import json as json_module

        reasons = {rule.name: rule_block_reason(rule, ctx) for rule in rules}
        store.transition(
            task_id,
            "completed",
            json_module.dumps({"auto_apply_blocked": reasons}, ensure_ascii=True),
        )
        return None
    rule, note = matched
    if patch_path is None or not patch_path.is_file():
        return None

    actor = f"auto:{rule.name}"
    # v40-F8: decided_by joins resolved_by="auto:<rule>" — one vocabulary,
    # two columns during the transition.
    decided_by = f"auto/{rule.name}"
    target_branch = branch or f"skep/{task_id}"
    verified_base = store.verified_base(task_id)
    moved = moved_past_verified_base(repo, target_branch, verified_base)
    if moved is not None and verified_base is not None:
        # ES-F3 (B131, I2): no human reads a warning on this lane, so it never
        # appends past the commit G10 verified — the run waits for a hand land.
        import json as json_module

        store.transition(
            task_id,
            "completed",
            json_module.dumps(
                {
                    "auto_apply_blocked": {
                        rule.name: f"{target_branch} is at {moved[:12]}, past the commit "
                        f"re-verified ({verified_base[:12]}); land it by hand"
                    }
                },
                ensure_ascii=True,
            ),
        )
        return None
    failure = apply_patch_on_branch(
        repo, target_branch, patch_path, task_id=task_id, actor=actor, verified_base=verified_base
    )
    review_id = store.enqueue_approval(
        task_id, action="apply_patch", reason=note, decided_by=decided_by
    )
    if failure is not None:
        # v81-F2: matched but could not apply. Approving again would fail the
        # same way, so a pending row is a lie (I8) — deny it with the failure
        # so the ledger says what happened; a re-run mints a fresh patch.
        store.resolve_approval(
            review_id, approved=False, actor=actor, note=f"apply failed: {failure}"
        )
        return None
    store.resolve_approval(
        review_id, approved=True, actor=actor, note=note, landing_branch=target_branch
    )
    return AutoApproval(rule_name=rule.name, review_id=review_id, note=note, branch=target_branch)
