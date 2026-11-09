"""The locked baseline (first-run O2, ADR 0056 D1/D2).

The approved policy template is written once under the home and locked by a
SHA-256 recorded at approval time. Three files:

- ``<home>/policy/baseline.json`` — the layer, in the O1 template format.
- ``<home>/policy/baseline.lock`` — the approval row plus the approved layer
  (the copy ``diff`` compares against).
- ``<home>/audit/policy/baseline-approvals.jsonl`` — append-only, one
  ``baseline_approved`` row per approval and one ``baseline_tampered`` row
  per tamper event (the debounce and the "since" time). Not ``approvals``:
  that word is the v79 patch-approval table (I13).

The hash is over ``canonical_bytes`` — sorted-key JSON of the *loaded* layer,
carrying ``schema_version`` — so reformatting the file changes nothing and
changing a setting changes the hash. The file is also set ``0444``; that is a
courtesy against slips, not the enforcement: workers and the operator are the
same Unix user, so the hash and the ledger are what make a change evident.

``verify`` is pure file I/O, cached by the files' mtimes; the reader
(``policy_layers``) is the one caller that turns its answer into a layer.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .policy_engine import Layer
from .policy_templates import PolicyTemplate

BASELINE_TAMPERED = "baseline_tampered"
# O5 (ADR 0056 D2) flips this to "no_baseline": a home with nothing approved
# then answers `ask`. Until then the zero state is exactly today's behaviour.
NO_BASELINE_REASON: str | None = None
LEDGER_NAME = "baseline-approvals"


def baseline_path(home: Path) -> Path:
    return home / "policy" / "baseline.json"


def lock_path(home: Path) -> Path:
    return home / "policy" / "baseline.lock"


def ledger_path(home: Path) -> Path:
    # SupervisorConfig.audit_dir is always <home>/audit.
    return home / "audit" / "policy" / f"{LEDGER_NAME}.jsonl"


def canonical_bytes(layer: PolicyTemplate, *, schema_version: int | None = None) -> bytes:
    """Sorted-key JSON of the loaded layer. ``schema_version`` overrides the
    layer's own — how a lock written under an older schema is recognised."""
    document = layer.model_dump(mode="json")
    if schema_version is not None:
        document["schema_version"] = schema_version
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def sha256_of(layer: PolicyTemplate, *, schema_version: int | None = None) -> str:
    return hashlib.sha256(canonical_bytes(layer, schema_version=schema_version)).hexdigest()


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _append(home: Path, row: dict[str, Any]) -> None:
    path = ledger_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row) + "\n")


def _ledger(home: Path) -> list[dict[str, Any]]:
    path = ledger_path(home)
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue  # a torn line is not a row; the lock check still has to match one
        if isinstance(row, dict):
            rows.append(row)
    return rows


def write_lock(
    home: Path,
    layer: PolicyTemplate,
    *,
    approved_by: str,
    schema_version: int | None = None,
) -> dict[str, Any]:
    """Approve ``layer``: write the file (``0444``), the lock and the ledger
    row. Returns the row. ``schema_version`` is for tests that stand in for
    an older skep's lock."""
    from .. import __version__

    version = layer.schema_version if schema_version is None else schema_version
    row: dict[str, Any] = {
        "kind": "baseline_approved",
        "sha256": sha256_of(layer, schema_version=version),
        "template": layer.template,
        "approved_by": approved_by,
        "approved_at": _now(),
        "skep_version": __version__,
        "schema_version": version,
    }
    path = baseline_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        os.chmod(path, 0o644)
    path.write_text(json.dumps(layer.model_dump(mode="json"), indent=2) + "\n", encoding="utf-8")
    os.chmod(path, 0o444)
    lock = {**row, "layer": layer.model_dump(mode="json")}
    lock_path(home).write_text(json.dumps(lock, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _append(home, row)
    _CACHE.pop(home, None)
    return row


# --- verify -------------------------------------------------------------------


@dataclass(frozen=True)
class Ok:
    layer: PolicyTemplate
    lock: dict[str, Any]
    # The lock was written under an older schema and the content is what was
    # approved: the layer applies and a card asks for re-approval.
    schema_from: int | None = None

    def enforced_layer(self) -> Layer:
        """The template layer with every set setting enforced (ADR 0053 D4)."""
        base = self.layer.layer()
        return replace(base, settings=tuple(replace(s, enforced=True) for s in base.settings))


@dataclass(frozen=True)
class Tampered:
    diff: tuple[str, ...]


@dataclass(frozen=True)
class Missing:
    """``baseline.json`` exists and its lock does not — treated as tampered."""

    diff: tuple[str, ...] = ("baseline.lock is missing",)


@dataclass(frozen=True)
class NoBaseline:
    """Nothing approved, ever — the zero state (O5 decides what it answers)."""


BaselineState = Ok | Tampered | Missing | NoBaseline

_CACHE: dict[Path, tuple[tuple[tuple[int, int] | None, ...], BaselineState]] = {}


def _stamp(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size)


def verify(home: Path) -> BaselineState:
    """Ok | Tampered(diff) | Missing | NoBaseline, recomputed whenever a
    file's mtime (or size) changes."""
    key = tuple(_stamp(p) for p in (baseline_path(home), lock_path(home), ledger_path(home)))
    cached = _CACHE.get(home)
    if cached is not None and cached[0] == key:
        return cached[1]
    state = _verify(home)
    _CACHE[home] = (key, state)
    return state


def _load_layer(raw: Any) -> PolicyTemplate:
    if isinstance(raw, str):
        return PolicyTemplate.model_validate_json(raw)
    return PolicyTemplate.model_validate(raw)


def _verify(home: Path) -> BaselineState:
    approvals = [row for row in _ledger(home) if row.get("kind") == "baseline_approved"]
    file, lock_file = baseline_path(home), lock_path(home)
    if not file.exists() and not lock_file.exists():
        if approvals:
            # Deleting both files must not be a way back to "nothing approved".
            return Tampered(
                ("baseline.json and baseline.lock are gone; the ledger has an approval",)
            )
        return NoBaseline()
    if not lock_file.exists():
        return Missing()
    try:
        lock = json.loads(lock_file.read_text(encoding="utf-8"))
        approved = _load_layer(lock["layer"])
    except (OSError, ValueError, KeyError, TypeError, ValidationError) as exc:
        return Tampered((f"baseline.lock is unreadable: {exc}",))
    lock_sv = lock.get("schema_version")
    if lock.get("sha256") != sha256_of(approved, schema_version=lock_sv):
        return Tampered(("baseline.lock was edited: its layer does not hash to its sha256",))
    if not approvals or approvals[-1].get("sha256") != lock["sha256"]:
        return Tampered(("baseline.lock does not match the last row of the approvals ledger",))
    if not file.exists():
        return Tampered(("baseline.json is missing",))
    try:
        current = _load_layer(file.read_text(encoding="utf-8"))
    except (OSError, ValueError, ValidationError) as exc:
        return Tampered((f"baseline.json no longer loads: {exc}",))
    if sha256_of(current) == lock["sha256"]:
        return Ok(current, lock)
    if (
        current.schema_version != lock_sv
        and sha256_of(current, schema_version=lock_sv) == (lock["sha256"])
    ):
        return Ok(current, lock, schema_from=lock_sv)
    return Tampered(tuple(layer_diff(approved, current)))


def layer_diff(old: PolicyTemplate, new: PolicyTemplate) -> list[str]:
    """What changed between two layers: settings by key, rules by rule_id."""
    lines: list[str] = []
    for field in ("template", "pack", "schema_version"):
        if getattr(old, field) != getattr(new, field):
            lines.append(f"~ {field}: {getattr(old, field)!r} -> {getattr(new, field)!r}")
    for key in sorted(old.settings.keys() | new.settings.keys()):
        if key not in new.settings:
            lines.append(f"- setting {key}: {json.dumps(old.settings[key])}")
        elif key not in old.settings:
            lines.append(f"+ setting {key}: {json.dumps(new.settings[key])}")
        elif old.settings[key] != new.settings[key]:
            lines.append(
                f"~ setting {key}: {json.dumps(old.settings[key])} -> "
                f"{json.dumps(new.settings[key])}"
            )

    def rules(layer: PolicyTemplate) -> dict[str, str]:
        return {r.rule_id: f"{r.scope}: {r.verdict} {r.action} {r.pattern}" for r in layer.rules}

    before, after = rules(old), rules(new)
    for rule_id in sorted(after.keys() - before.keys()):
        lines.append(f"+ rule {rule_id}: {after[rule_id]}")
    for rule_id in sorted(before.keys() - after.keys()):
        lines.append(f"- rule {rule_id}: {before[rule_id]}")
    for rule_id in sorted(before.keys() & after.keys()):
        if before[rule_id] != after[rule_id]:
            lines.append(f"~ rule {rule_id}: {before[rule_id]} -> {after[rule_id]}")
    if not lines and canonical_bytes(old) != canonical_bytes(new):
        lines.append("~ rule order changed")  # same content, different hash: order is content
    return lines


def locked_layer(home: Path) -> PolicyTemplate | None:
    """The layer the lock says was approved, or None when there is no
    readable lock — what ``explain baseline`` shows and ``--reapprove``
    diffs against."""
    try:
        return _load_layer(json.loads(lock_path(home).read_text(encoding="utf-8"))["layer"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def current_layer(home: Path) -> PolicyTemplate:
    """``baseline.json`` as it is now — what a bare ``--reapprove`` re-locks."""
    try:
        return _load_layer(baseline_path(home).read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError("no baseline.json to re-approve — name a --template or a --file") from exc
    except ValueError as exc:  # pydantic's ValidationError is a ValueError
        detail = (str(exc).splitlines()[1:2] or [str(exc)])[0].strip()
        raise ValueError(
            f"baseline.json no longer loads ({detail}) — fix it, or re-approve from a "
            "source: --template NAME or --file PATH"
        ) from exc


def fail_closed_reason(state: BaselineState) -> str | None:
    """The one place a baseline state becomes a verdict override (or not)."""
    if isinstance(state, Tampered | Missing):
        return BASELINE_TAMPERED
    if isinstance(state, NoBaseline):
        return NO_BASELINE_REASON
    return None


# --- tamper events: one card each, debounced across processes -----------------


def _event_key(home: Path, state: BaselineState) -> str:
    if isinstance(state, Ok):
        return f"schema:{state.schema_from}->{state.layer.schema_version}:{state.lock['sha256']}"
    try:
        return hashlib.sha256(baseline_path(home).read_bytes()).hexdigest()
    except OSError:
        return "baseline.json absent"


def _since_last_approval(home: Path) -> list[dict[str, Any]]:
    rows = _ledger(home)
    last = max((i for i, r in enumerate(rows) if r.get("kind") == "baseline_approved"), default=-1)
    return [r for r in rows[last + 1 :] if r.get("kind") == BASELINE_TAMPERED]


def note_event(home: Path, state: BaselineState) -> dict[str, Any] | None:
    """Record a tamper (or schema-change) event once; the row when it is new,
    None when this exact event was already recorded since the last approval."""
    if not isinstance(state, Tampered | Missing) and not (
        isinstance(state, Ok) and state.schema_from is not None
    ):
        return None
    key = _event_key(home, state)
    if any(row.get("key") == key for row in _since_last_approval(home)):
        return None
    if isinstance(state, Ok):
        diff = [f"schema_version {state.schema_from} -> {state.layer.schema_version}; re-approve"]
        reason = "schema_changed"
    else:
        diff, reason = list(state.diff), BASELINE_TAMPERED
    row = {"kind": BASELINE_TAMPERED, "reason": reason, "key": key, "at": _now(), "diff": diff}
    _append(home, row)
    return row


def tampered_since(home: Path) -> str | None:
    """When the current run of tamper events began (the first since approval)."""
    rows = [r for r in _since_last_approval(home) if r.get("reason") == BASELINE_TAMPERED]
    return str(rows[0]["at"]) if rows else None


def baseline_status(home: Path) -> dict[str, Any]:
    """The state on every face: ``skep policy baseline status``, ``GET
    /api/policy/baseline`` and the chat ``policy_explain baseline``."""
    state = verify(home)
    view: dict[str, Any] = {"state": "none", "diff": [], "tampered_since": None}
    if isinstance(state, Ok):
        view.update(
            state="ok" if state.schema_from is None else "reapprove",
            **{k: state.lock.get(k) for k in ("sha256", "template", "approved_by", "approved_at")},
            schema_version=state.lock.get("schema_version"),
        )
        if state.schema_from is not None:
            view["diff"] = [
                f"schema_version {state.schema_from} -> {state.layer.schema_version}; "
                "the content is what was approved — re-approve to re-lock"
            ]
    elif isinstance(state, Tampered | Missing):
        view.update(state="tampered", diff=list(state.diff), tampered_since=tampered_since(home))
    return view


def status_line(home: Path) -> str:
    """One line for ``why``: ``baseline: TAMPERED since <time>`` and friends."""
    view = baseline_status(home)
    if view["state"] == "tampered":
        return f"baseline: TAMPERED since {view['tampered_since'] or 'now'}"
    if view["state"] == "none":
        return "baseline: none approved"
    suffix = " (schema changed: re-approve)" if view["state"] == "reapprove" else ""
    return (
        f"baseline: {view['template']} approved {view['approved_at']} "
        f"sha256 {view['sha256'][:12]}{suffix}"
    )
