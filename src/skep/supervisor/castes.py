"""v101-F1 (ADR 0049): which worker runs a task — the roster as a registry.

The contract has declared seven worker kinds since v51 (``KNOWN_WORKER_KINDS``),
and the routing table that turns a name into a process was a dict literal inside
``build_config`` listing five. Every other surface kept its own copy: the Assign
form offered two, the chat tool enums two and three. Five lists, already
diverged — and one name, ``verifier``, declared in the contract and routable
nowhere, so ``config.command_for("verifier")`` fell through to the *coding*
worker and a verifier dispatch ran a coding worker under a verifier's name.

Code that exists but is never registered behaves exactly as if it does not
exist — the v42 / v51-F3 lesson, which ``engines.py`` states in its own opening
and which was then not applied to castes. This module is that fix, modelled
beat-for-beat on ``engines.py``: one table, one resolver that refuses an unknown
name instead of falling back, and one description string every surface reads.

**The contract stays authoritative for which names exist.**
``KNOWN_WORKER_KINDS`` is the vocabulary; this registry is the supervisor-side
routing and description table for it. Nothing imports across that line — the pin
that ties the two is a test (``test_castes.py``), not an import cycle.

**Not wired on purpose:** the schedules ``caste`` enum (``tools.py:1653``). It
mixes worker castes with supervisor-side schedule kinds and carries a real name
collision — a schedule of kind ``script`` runs a shell command on the
*supervisor host*, which is not the ``script`` worker caste at all. Feeding this
registry into that enum would silently redefine an existing verb.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

DEFAULT_CASTE = "coding"


@dataclass(frozen=True)
class Caste:
    """One worker caste the supervisor can route a task to."""

    name: str
    # The worker argv. EMPTY for ``coding``, which defers to
    # ``config.command_for`` — that is what ``SKEP_WORKER_CMD``, ``--worker-cmd``
    # and the test fake worker override, and a registry must not quietly take
    # that away (the ``BUILTIN_ENGINE`` precedent, engines.py:47-51).
    argv: tuple[str, ...]
    # One operator-facing line. The Settings roster and the chat tool schema
    # both read it (as ``summary``) — a caste described in two places drifts in
    # two places, which is the defect this module exists to end.
    about: str
    # Can this caste's output become a commit? False means the caste produces
    # artifacts only and has no path to a patch — the supervisor never offers
    # a landing for it, and saying so is how an operator knows what to expect.
    lands: bool
    # Does it call the assistant LLM? Drives the provider-host network merge
    # (v19-F2) and tells the operator which castes cost tokens.
    needs_provider: bool
    # Does it fetch? Only the researcher does; everything else is offline by
    # construction, which is what makes those castes gate-safe on their own.
    needs_network: bool
    # v127-F12: never run in the field (O9). Still routed — the CLI, templates
    # and schedules reach it — but the chat enums leave it out, so the Queen's
    # small model is not trained to reach for it. ``summary`` says so.
    advanced: bool = False

    @property
    def summary(self) -> str:
        """``about``, prefixed from the ``advanced`` flag (recorded once)."""
        return f"Advanced: {self.about}" if self.advanced else self.about


CASTES: dict[str, Caste] = {
    DEFAULT_CASTE: Caste(
        name=DEFAULT_CASTE,
        argv=(),  # defers to config.command_for (SKEP_WORKER_CMD / --worker-cmd)
        about="Writes code against a repo and produces a patch for approval.",
        lands=True,
        needs_provider=True,
        needs_network=False,
    ),
    "audit": Caste(
        name="audit",
        argv=(sys.executable, "-m", "skep.workers.audit"),
        about=(
            "Bumps unsafe dependency pins against an advisory set and re-runs the "
            "suite. Deterministic, LLM-free, offline."
        ),
        lands=True,
        needs_provider=False,
        needs_network=False,
        advanced=True,
    ),
    "curator": Caste(
        name="curator",
        argv=(sys.executable, "-m", "skep.workers.curator"),
        about=(
            "Turns an inbox of notes into memory PROPOSALS for review; never "
            "writes durable memory itself."
        ),
        lands=False,
        needs_provider=False,
        needs_network=False,
        advanced=True,
    ),
    "document": Caste(
        name="document",
        argv=(sys.executable, "-m", "skep.workers.document"),
        about="Drafts and summaries as deliverables. No web, no code, nothing lands.",
        lands=False,
        needs_provider=True,
        needs_network=False,
        advanced=True,
    ),
    "researcher": Caste(
        name="researcher",
        argv=(sys.executable, "-m", "skep.workers.researcher"),
        about=(
            "Answers a question from ALLOW-LISTED sources only, with evidence per "
            "source and the ones it could not reach named."
        ),
        lands=False,
        needs_provider=False,
        needs_network=True,
        advanced=True,
    ),
    "reviewer": Caste(
        name="reviewer",
        argv=(sys.executable, "-m", "skep.workers.reviewer"),
        about=(
            "Reads the diff against the startup baseline and reports findings "
            "with a verdict. Read-only; a review never lands."
        ),
        lands=False,
        needs_provider=True,
        needs_network=False,
        advanced=True,
    ),
    "script": Caste(
        name="script",
        argv=(sys.executable, "-m", "skep.workers.script_worker"),
        about=(
            "Runs one inline script in a sandboxed worktree with deny-all egress. "
            "Produces output, never a patch."
        ),
        lands=False,
        needs_provider=False,
        needs_network=False,
    ),
}

# v101-F1 declared one hole here — ``verifier``, in KNOWN_WORKER_KINDS since v17
# and never written. v101-F2 wrote the worker and registered it, so the set is
# empty. It stays as a named concept because the NEXT caste added to the
# contract without a worker needs somewhere honest to sit for one commit (I8).
UNIMPLEMENTED_CASTES: frozenset[str] = frozenset()

# v127-F11: contract names with no worker ON PURPOSE, each with the reason a
# refusal gives. The contract is additive, so a retired name stays in
# KNOWN_WORKER_KINDS; this is where the supervisor says why it won't run it.
# ``verifier`` re-ran the pin G10 already re-runs on every patch, and never
# ran in the field (I2: the supervisor re-verifies, not a caste).
RETIRED_CASTES: dict[str, str] = {
    "verifier": "skep re-runs the project's pinned verify_command on every patch (G10)",
}


def caste_names() -> list[str]:
    return sorted(CASTES)


def chat_caste_names() -> list[str]:
    """v127-F12: what the chat dispatch enums offer — the non-advanced castes."""
    return [name for name in caste_names() if not CASTES[name].advanced]


def resolve_caste(name: str | None) -> Caste:
    """The caste for ``name``; the coding worker when unset.

    Raises ValueError naming the valid choices — an unknown caste must never
    fall back silently to the coding worker. That is exactly what v42 found: an
    unregistered caste ran a coding worker and the run was rejected downstream
    with no useful reason (I9).
    """
    if not name:
        return CASTES[DEFAULT_CASTE]
    if name in RETIRED_CASTES:
        raise ValueError(f"caste {name!r} is retired: {RETIRED_CASTES[name]}")
    caste = CASTES.get(name)
    if caste is None:
        raise ValueError(f"unknown caste {name!r}; known: {', '.join(caste_names())}")
    return caste


def caste_worker_commands() -> dict[str, tuple[str, ...]]:
    """The routing table ``build_config`` installs on ``SupervisorConfig``.

    ``coding`` is omitted deliberately: an empty argv would override the
    configured worker command, and ``config.command_for`` already falls back to
    it for any name it does not hold.
    """
    return {caste.name: caste.argv for caste in CASTES.values() if caste.argv}
