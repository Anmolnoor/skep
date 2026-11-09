"""SKEP_UNLOCKED — a reversible capability probe that instruments the policy layer.

Set ``SKEP_UNLOCKED=1`` (or put the line in ``~/.skep/serve.env``, which
``skep serve`` loads at boot) to run the sandbox tier: the friction gates that
are *contained by the sandbox* — dispatch cards, worker escalations, the
verified-patch landing click, and run egress — auto-resolve, while every gate
records what it WOULD have decided to an append-only JSONL. The never-grantable
deny floor (git push/pull/fetch, sudo, catastrophic, workspace-escape) and the
safety-critical landing checks (G10 re-verify, landing-branch validation, the
sandbox wall) are NEVER flipped — they are logged, so a deny in the report is
exactly the signal "this gate protects something real".

Default OFF ⇒ every call site here is a dead branch ⇒ governed skep is
byte-for-byte main. This is the reversibility guarantee. FOR LOCAL CAPABILITY
TESTING ONLY — never production.
"""

from __future__ import annotations

import json
import os
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# host/all are accepted as the sandbox tier for now: this build only implements
# the sandbox-contained bypasses. The host tier (also flipping outbound content,
# Queen host shell, policy mutations) is documented as future work; until it
# lands, an operator asking for it gets the working sandbox probe, not silent
# governed mode.
_TIER_ON = {"1", "true", "on", "yes", "sandbox", "host", "all"}


def unlocked_tier() -> str:
    """ "sandbox" when the env toggle is on, else "off" (unknown values fail closed)."""
    return "sandbox" if os.environ.get("SKEP_UNLOCKED", "").strip().lower() in _TIER_ON else "off"


def unlocked_active() -> bool:
    return unlocked_tier() != "off"


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _session_id() -> str:
    # Stable per serve/CLI process; the JSONL file is named for it so a report
    # can scope to one run of the daemon. pid is enough for a local probe.
    return f"pid{os.getpid()}"


def _log_dir(audit_dir: Path) -> Path:
    return Path(audit_dir) / "unlocked"


def record_gate(
    audit_dir: Path,
    *,
    gate: str,
    surface: str,
    would: str,
    reason: str,
    action: dict[str, Any],
    disposition: str,
    tier: str | None = None,
    run_id: str = "queen",
) -> None:
    """Append one gate event to ``<audit>/unlocked/<session>.jsonl``.

    Best-effort: instrumentation must never break a run. A failed write is a
    silent miss (fail open on the LOG, never on a decision) — the decision it
    records already happened by the time we get here.

    ``would`` is the gate's real verdict (allow | ask | deny). ``disposition``
    says what unlocked did with it: ``bypassed`` (a wall it skipped),
    ``logged_only`` (recorded, decision honored), ``allowed`` (ran in-turn for a
    reason other than unlocked, e.g. a standing grant).
    """
    try:
        directory = _log_dir(audit_dir)
        directory.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": _now_iso(),
            "session": _session_id(),
            "run_id": run_id,
            "gate": gate,
            "surface": surface,
            "would": would,
            "reason": reason,
            "tier": tier or unlocked_tier(),
            "action": action,
            "disposition": disposition,
        }
        with (directory / f"{_session_id()}.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True) + "\n")
    except Exception:
        pass


def classify_decision(decision: Any) -> tuple[str, str, str]:
    """(would, disposition, reason) for a Queen ``mutation_execution_decision``.

    ``decision`` is an ``AutonomyDecision`` or ``None`` (None ⇒ the verb cards).
    Duck-typed so this module stays import-cheap.
    """
    if decision is None:
        return "ask", "logged_only", "card.default"
    reason = getattr(decision, "reason", None) or "card.default"
    if getattr(decision, "verdict", None) == "deny":
        return "deny", "logged_only", reason
    allows = getattr(decision, "allows_execution", None)
    if callable(allows) and allows():
        by = f"{getattr(decision, 'decided_by', '') or ''} {reason}"
        return "allow", ("bypassed" if "unlocked" in by else "allowed"), reason
    return "ask", "logged_only", reason


def _compact_args(args: dict[str, Any]) -> dict[str, Any]:
    """A small, readable slice of a tool's args for the log — no giant prompts."""
    keep = ("repo", "task_id", "review_id", "branch", "base", "url", "server_id", "tool", "proc_id")
    out: dict[str, Any] = {k: args[k] for k in keep if k in args and args[k] not in (None, "")}
    command = args.get("command")
    if isinstance(command, str) and command:
        out["command"] = command[:200]
    return out


def record_mutation_gate(
    audit_dir: Path, *, chat_id: str, name: str, args: dict[str, Any], decision: Any
) -> None:
    """Instrument one Queen mutating-verb attempt at the chat card gate."""
    would, disposition, reason = classify_decision(decision)
    record_gate(
        audit_dir,
        gate=name,
        surface="queen_card",
        would=would,
        reason=reason,
        action={"verb": name, "args": _compact_args(args)},
        disposition=disposition,
        run_id=str(chat_id),
    )


def unlocked_banner(tier: str) -> str:
    line = "=" * 70
    return (
        f"\n{line}\n"
        f"  ⚠  SKEP UNLOCKED — GOVERNANCE BYPASSED  (tier={tier})\n"
        f"  Capability probe for LOCAL testing ONLY. NEVER production.\n"
        f"  Sandbox-contained approvals auto-allow and are logged;\n"
        f"  the never-grantable deny floor still denies (and is logged).\n"
        f"  Log:    <home>/supervisor/audit/unlocked/\n"
        f"  Report: skep unlocked-report\n"
        f"{line}"
    )


# ---- report -------------------------------------------------------------


def _read_events(audit_dir: Path, session: str | None) -> list[dict[str, Any]]:
    directory = _log_dir(audit_dir)
    if not directory.is_dir():
        return []
    pattern = f"{session}.jsonl" if session else "*.jsonl"
    events: list[dict[str, Any]] = []
    for path in sorted(directory.glob(pattern)):
        for raw in path.read_text(encoding="utf-8").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                events.append(json.loads(raw))
            except json.JSONDecodeError:
                continue
    return events


def _samples(events: list[dict[str, Any]], limit: int = 3) -> str:
    seen: list[str] = []
    for event in events:
        action = event.get("action") or {}
        verb = action.get("verb") or event.get("gate", "?")
        detail = action.get("args") or {k: v for k, v in action.items() if k != "verb"}
        label = f"{verb} {json.dumps(detail, sort_keys=True)}" if detail else str(verb)
        if label not in seen:
            seen.append(label)
        if len(seen) >= limit:
            break
    return " · ".join(seen)


def build_report(audit_dir: Path, *, session: str | None = None) -> str:
    events = _read_events(audit_dir, session)
    if not events:
        where = f" for session {session}" if session else ""
        return f"No unlocked-mode gate events found{where} under {_log_dir(audit_dir)}.\n"

    by_gate: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        by_gate[str(event.get("gate", "?"))].append(event)
    runs = {str(e.get("run_id")) for e in events}
    sessions = {str(e.get("session")) for e in events}

    def _gate_woulds(gate_events: list[dict[str, Any]]) -> set[str]:
        return {str(e.get("would")) for e in gate_events}

    lines: list[str] = [
        f"UNLOCKED REPORT — {len(events)} gate hits across {len(runs)} run(s), "
        f"{len(sessions)} session(s)",
        "",
    ]

    denied = [e for e in events if e.get("would") == "deny"]
    lines.append("WOULD HAVE DENIED (protecting something real — do NOT relax):")
    if denied:
        by_deny: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for event in denied:
            by_deny[str(event.get("gate"))].append(event)
        for gate, gate_events in sorted(by_deny.items(), key=lambda kv: -len(kv[1])):
            lines.append(f"  {gate:<30} {len(gate_events):>3}x   {_samples(gate_events)}")
    else:
        lines.append("  (none — no gate returned a deny while unlocked)")

    bypassed: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        if event.get("disposition") == "bypassed":
            bypassed[str(event.get("gate"))].append(event)
    lines += ["", "AUTO-ALLOWED BY UNLOCKED (a wall it skipped — friction, ranked):"]
    if bypassed:
        for gate, gate_events in sorted(bypassed.items(), key=lambda kv: -len(kv[1])):
            lines.append(f"  {gate:<30} {len(gate_events):>3}x   {_samples(gate_events)}")
    else:
        lines.append("  (none)")

    still_asking: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        if event.get("would") == "ask" and event.get("disposition") == "logged_only":
            still_asking[str(event.get("gate"))].append(event)
    lines += ["", "STILL ASKED (carded even under the sandbox tier — host-tier candidates):"]
    if still_asking:
        for gate, gate_events in sorted(still_asking.items(), key=lambda kv: -len(kv[1])):
            lines.append(f"  {gate:<30} {len(gate_events):>3}x   {_samples(gate_events)}")
    else:
        lines.append("  (none)")

    never_denied = sorted(
        gate for gate, gate_events in by_gate.items() if "deny" not in _gate_woulds(gate_events)
    )
    lines += ["", "NEVER DENIED (only ever ask/allow — safe to relax in governed mode):"]
    lines.append("  " + (", ".join(never_denied) if never_denied else "(none)"))
    lines.append("")
    return "\n".join(lines) + "\n"


def cmd_unlocked_report(args: Any) -> int:
    """``skep unlocked-report`` — read the JSONL log back as a grouped summary."""
    from .cli_cmds import build_config

    audit_dir = build_config(args.home, None).audit_dir
    print(build_report(audit_dir, session=getattr(args, "session", None)), end="")
    return 0
