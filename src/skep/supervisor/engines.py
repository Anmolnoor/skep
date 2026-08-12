"""v90-F1 (ADR 0047): which coding agent runs a coding task.

skep ships its own coding worker and three adapters over external agent CLIs —
Claude Code, Codex, Aider — all complete since v33 and all, until now,
unreachable: nothing mapped a name to them, so the only way to run Claude Code
was to replace the default worker process-wide with ``SKEP_WORKER_CMD``. Code
that exists but is never registered behaves exactly as if it does not exist
(the v42 / v51-F3 lesson).

**The boundary, stated (I12).** An external agent is NOT confined the way
skep's own workers are. ``cli_adapter.py`` runs the binary with a plain
``subprocess.run``, so the worker-side git guards (``runtime_plugins.py`` —
v19-F3/F5, v22-F2) never see its commands: those live in ``shell.run``
capability handling, which only first-party workers route through. What still
binds a CLI engine is the **sandbox** — workspace-only writes and the per-task
network pin, inherited because the whole worker process tree runs under
bubblewrap/Seatbelt. So for a CLI-engine run:

- it can ``git commit`` inside its own worktree — harmless for the reason
  v88-F5 gave: the tree is destroyed and the patch is a working-tree diff;
- it cannot reach a remote, because the network pin does not include one;
- I1 is untouched — the patch still lands only through a human approval.

**Verification is not optional here.** A CLI engine's built-in verification is
``git diff --check`` (``cli_adapter.py``) — a whitespace and conflict-marker
check that says nothing about whether the work is correct. Re-verifying that
under G10 re-runs the whitespace check. So a CLI engine requires the project to
pin a real ``verify_command`` (v88-F4); this is the one place that opt-in
becomes mandatory, because the vacuous verify is the adapter's design rather
than a worker's bad choice.
"""

from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass

BUILTIN_ENGINE = "builtin"


@dataclass(frozen=True)
class CodingEngine:
    """One coding-agent implementation the operator may choose."""

    name: str
    # The argv that REPLACES the configured coding worker. Empty for the
    # built-in engine, which defers to ``config.command_for`` — that is what
    # ``SKEP_WORKER_CMD``, ``--worker-cmd`` and the test fake worker override,
    # and an engine must not quietly take that away.
    argv: tuple[str, ...]
    # The binary probed by ``skep doctor``. None for the built-in worker, which
    # is this interpreter and always present.
    binary: str | None = None
    # The host the engine must reach to function at all. Merged into the run's
    # network allowlist the way v19-F2 merges the LLM provider host: an agent
    # that cannot reach its API cannot work, and the failure without this is a
    # confusing timeout instead of a stated denial (I12).
    network_host: str | None = None
    # v94-F3: the env vars the engine cannot function without, merged into the
    # run's env allowlist the same way. The worker baseline is PATH+HOME only;
    # Claude Code's macOS keychain credential lookup additionally needs
    # USER/LOGNAME — without them every run dies on "Not logged in". Identity
    # names, never secrets; the registry naming them keeps G2 intact.
    env_vars: tuple[str, ...] = ()
    # v106-F1: runtime state the engine must be able to WRITE, declared as
    # (env var, subdirectory) pairs resolved under the run's workspace-local
    # ``.toolchain/`` scratch dir. The sandbox makes only the workspace
    # writable (I12); Claude Code writes session-env/shell snapshots under
    # ``~/.claude``, so without this its Bash tool dies on a read-only mount
    # and every shell-needing task ends "completed but produced no patch".
    toolchain_env: tuple[tuple[str, str], ...] = ()
    # v111-F3: headless auth — at least ONE of these env vars must be set in
    # the supervisor's OWN process env for the engine to authenticate at all:
    # file-based logins (~/.claude/.credentials.json, ~/.codex/auth.json)
    # never reach the sandbox once the config dir is redirected per-run
    # (v106-F1), so the environment is the only carrier. Names only, never
    # values (G2); ``skep doctor`` compares them against the process env —
    # the 2026-08-11 restart shed the token and doctor still said "ok".
    auth_env: tuple[str, ...] = ()
    # True when the agent's own commands do NOT pass skep's capability layer.
    external: bool = True
    summary: str = ""


CODING_ENGINES: dict[str, CodingEngine] = {
    BUILTIN_ENGINE: CodingEngine(
        name=BUILTIN_ENGINE,
        argv=(),  # defers to config.command_for (SKEP_WORKER_CMD / --worker-cmd)
        binary=None,
        network_host=None,
        external=False,
        summary="skep's own worker — every action passes the capability layer.",
    ),
    "claude_code": CodingEngine(
        name="claude_code",
        argv=(sys.executable, "-m", "skep.workers.claude_code"),
        binary="claude",
        network_host="api.anthropic.com",
        # CLAUDE_CODE_OAUTH_TOKEN / ANTHROPIC_API_KEY: v106-F1's per-run
        # CLAUDE_CONFIG_DIR redirect orphans Linux file-based logins
        # (~/.claude/.credentials.json never reaches the run — the authwapi
        # acceptance died on "Not logged in" in 1s). Headless auth rides the
        # environment instead: `claude setup-token` mints the long-lived
        # token. Names only (G2); unset vars are inert allowlist entries.
        env_vars=("USER", "LOGNAME", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"),
        auth_env=("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"),
        toolchain_env=(("CLAUDE_CONFIG_DIR", "claude"),),
        summary=(
            "Claude Code, headless (--print). Confined by the sandbox, not the capability layer."
        ),
    ),
    "codex": CodingEngine(
        name="codex",
        argv=(sys.executable, "-m", "skep.workers.codex"),
        binary="codex",
        network_host="api.openai.com",
        # v111-F4: the entry predated the v94-F3/v106-F1 hardening and never
        # got it — zero codex runs ever completed. ~/.codex/auth.json (the
        # ChatGPT login) is file-based and can never reach the sandbox once
        # CODEX_HOME is redirected per-run; auth rides OPENAI_API_KEY or not
        # at all — the same story as claude_code's /login. The redirect also
        # gives codex's own sqlite state a writable home: under the read-only
        # / mount it dies opening ~/.codex before auth is even tested (I12).
        env_vars=("USER", "LOGNAME", "OPENAI_API_KEY"),
        auth_env=("OPENAI_API_KEY",),
        toolchain_env=(("CODEX_HOME", "codex"),),
        summary=("Codex CLI, headless. Confined by the sandbox, not the capability layer."),
    ),
    "aider": CodingEngine(
        name="aider",
        argv=(sys.executable, "-m", "skep.workers.aider"),
        binary="aider",
        network_host=None,  # provider depends on the operator's aider config
        summary=(
            "Aider, pinned --no-auto-commit. Confined by the sandbox, not the capability layer."
        ),
    ),
    "pi": CodingEngine(
        name="pi",
        argv=(sys.executable, "-m", "skep.workers.pi"),
        binary="pi",
        network_host=None,  # multi-provider; the operator's provider host rides v19-F2
        # pi reads whichever provider key matches its configured model; unset
        # names are inert allowlist entries (G2 — names, never secrets).
        env_vars=("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY"),
        # pi writes config + session transcripts under ~/.pi/agent; the sandbox
        # makes only the workspace writable (v106-F1), and the redirect doubles
        # as evidence — the transcript survives with a kept worktree (v107-F1).
        toolchain_env=(("PI_CODING_AGENT_DIR", "pi"),),
        summary=("pi, headless (-p). Confined by the sandbox, not the capability layer."),
    ),
}


def engine_names() -> list[str]:
    return sorted(CODING_ENGINES)


def resolve_engine(name: str | None) -> CodingEngine:
    """The engine for ``name``; the built-in worker when unset.

    Raises ValueError naming the valid choices — an unknown engine must never
    fall back silently to the coding worker (v42: an unregistered caste did
    exactly that and the run was rejected downstream with no useful reason).
    """
    if not name:
        return CODING_ENGINES[BUILTIN_ENGINE]
    engine = CODING_ENGINES.get(name)
    if engine is None:
        raise ValueError(f"unknown coding_engine {name!r}; known: {', '.join(engine_names())}")
    return engine


def engine_available(engine: CodingEngine) -> tuple[bool, str]:
    """(present, detail) for ``skep doctor``.

    The v87-F6 lesson: the operator allowlisted a binary that did not exist on
    the host and burned three runs before anything said so. An engine is
    reported absent BEFORE it is dispatched, with the name that was probed.
    """
    if engine.binary is None:
        return True, f"{sys.executable} (skep's own worker)"
    found = shutil.which(engine.binary)
    if found is None:
        return False, f"{engine.binary!r} not on PATH"
    return True, found
