"""Phase 08 8.1: verify the hash-chained decision log and export a local receipt (I8, I13).

``verify_ledger`` walks every generation of ``decisions.jsonl`` oldest first
and names the first break by file and line. ``build_receipt`` is one run's
verifiable LOCAL receipt: identity, posture, G10/V1, and the ledger head at
export. Both are reads: nothing here opens a file for writing.

What the chain proves, and what it does not (I8): every ``"v": 2`` row names
the hash of the row before it, so an edited, inserted, deleted or reordered
row inside the log — or a missing oldest generation — is a break. A rewrite
of the WHOLE log, or a cut of its newest rows, still links; the checkpoints
in ``chain.sqlite3`` catch either only when they were not edited in step,
and both live in the same home. The only anchor outside this machine is the
head hash, once the receipt is pasted somewhere public (a PR, a commit).
"""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import re
import sqlite3
from collections import Counter
from collections.abc import Iterator
from contextlib import closing, contextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

from .. import __version__
from . import policy_v3
from .policy_baseline import baseline_status
from .policy_v3 import CHAIN_DB, GENESIS, _rotated_generations, chain_hash, redact_export
from .store import RunStore

SCOPE = (
    "Local receipt, not tamper-proof: the hash chain shows the decision log was not "
    "edited, reordered or cut in the middle since it was written, and the head hash pins "
    "it as of export. Whoever can write the skep home can rewrite the whole log and its "
    "checkpoints, so the anchor outside this machine is this receipt where you publish it "
    "(a PR description or a commit)."
)
_CHAIN_KEYS = {"seq", "prev_hash", "hash"}


def _lines(handle: IO[bytes], size: int) -> Iterator[bytes]:
    """The file's lines, each with its newline, up to ``size`` bytes — its
    length at the snapshot."""
    read = 0
    for raw in handle:
        if read >= size:
            return
        read += len(raw)
        yield raw


def _checkpoints(db: Path) -> dict[int, str]:
    if not db.exists():
        return {}
    # Review R1 (Q7): a URI, so a "?" or "#" in the home's path is a path
    with closing(sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        return dict(conn.execute("SELECT seq, hash FROM checkpoints"))


@contextmanager
def _shared_lock(path: Path) -> Iterator[None]:
    """A shared flock on ``decisions.lock`` (writers take it exclusively)."""
    try:
        lock = path.open("rb")
    except FileNotFoundError:
        yield  # no writer has ever run
        return
    with lock:
        fcntl.flock(lock, fcntl.LOCK_SH)
        yield


@dataclass
class _Walk:
    """The walk so far, file after file: the head, the counts, and each run's rows."""

    checkpoints: dict[int, str]
    anchored: bool
    head: dict[str, Any] | None = None
    unchained: int = 0
    chained: int = 0
    starts_at: int | None = None
    tasks: Counter[str] = field(default_factory=Counter)

    def file(self, path: Path, handle: IO[bytes], size: int) -> dict[str, Any] | None:
        """Walk one file on; the first break in it, or None."""
        for line, raw in enumerate(_lines(handle, size), 1):
            where = {"file": str(path), "line": line}
            try:
                row = json.loads(raw)
            except ValueError:
                row = None
            if not isinstance(row, dict):
                return {**where, "reason": "the line is not a JSON row skep wrote"}
            if row.get("v") != 2:
                if self.head is not None:
                    return {**where, "reason": "an unchained line (v1) inside the chain"}
                # Review R1 (Q1): only a v1 (or older) row, chain keys and all
                # absent, is an old row the chain starts after. Review R2 (F1):
                # v1 is a missing "v" or the int 1 — not null, true or 1.0
                old = row.get("v", 1)
                if type(old) is not int or old != 1 or _CHAIN_KEYS & row.keys():
                    return {
                        **where,
                        "reason": f"a row with version {row.get('v')!r} and chain keys "
                        f"{sorted(_CHAIN_KEYS & row.keys())} is neither an old v1 row nor chained",
                    }
                self.unchained += 1
                self._count(row)
                continue
            # Review R2 (F1): the exact bytes log_decision writes, key order included
            if raw != (json.dumps(row, sort_keys=True) + "\n").encode():
                return {**where, "reason": "the line is not the bytes skep wrote"}
            stored, seq = row.pop("hash", None), row.get("seq")
            if type(seq) is not int:
                return {**where, "reason": "the row has no integer seq"}
            if stored != chain_hash(row):
                return {**where, "reason": f"seq {seq}'s hash does not match its content"}
            if self.head is None:
                # Review R1 (Q1): rows from before the chain mean this is the
                # log's start, even in an offline copy
                if (self.anchored or self.unchained) and (
                    seq != 1 or row.get("prev_hash") != GENESIS
                ):
                    return {
                        **where,
                        "reason": f"the chain starts at seq {seq}, not at seq 1 from GENESIS: "
                        "earlier rows (or a whole generation) are missing",
                    }
                self.starts_at = seq
            elif seq != self.head["seq"] + 1:
                return {
                    **where,
                    "reason": f"seq {seq} follows seq {self.head['seq']}: "
                    "a row is missing or moved",
                }
            elif row.get("prev_hash") != self.head["hash"]:
                return {
                    **where,
                    "reason": f"seq {seq}'s prev_hash is not seq {self.head['seq']}'s hash",
                }
            if self.checkpoints.get(seq, stored) != stored:
                return {**where, "reason": f"seq {seq} differs from its checkpoint in {CHAIN_DB}"}
            self.head = {"seq": seq, "hash": stored, "file": str(path), "line": line}
            self.chained += 1
            self._count(row)
        return None

    def _count(self, row: dict[str, Any]) -> None:
        if isinstance(task := row.get("task_id"), str):
            self.tasks[task] += 1

    def covered(self) -> dict[int, str]:
        """The checkpoints inside the chain walked so far."""
        top = 0 if self.head is None else self.head["seq"]
        return {seq: digest for seq, digest in self.checkpoints.items() if seq <= top}


# Review R1 (Q6): a rotated generation is never written again, so the walk
# through all of them is kept per log, keyed by every generation's identity
# (inode, size, mtime, ctime: an edit changes the ctime) and checked against
# the checkpoints inside it. Forensics and receipts then hash only the live file.
# ponytail: one entry per log in this process; a new generation re-walks once.
_VERIFIED: dict[str, tuple[tuple[Any, ...], _Walk]] = {}


def _identity(path: Path, stat: os.stat_result) -> tuple[Any, ...]:
    return (str(path), stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def _generations(
    checkpoints: dict[int, str], key: str, generations: list[Path]
) -> tuple[_Walk, dict[str, Any] | None]:
    """Walk the rotated generations — one open at a time (a descriptor each
    ran a long-lived home out of them) — or reuse the last walk of these
    same files when no checkpoint inside them changed."""
    seen = []
    for each in generations:
        try:
            seen.append(_identity(each, each.stat()))
        except FileNotFoundError:
            continue
    kept = _VERIFIED.get(key)
    if kept is not None and seen and kept[0] == tuple(seen):
        walk = copy.deepcopy(kept[1])
        walk.checkpoints = checkpoints
        if walk.covered() == kept[1].covered():
            return walk, None
    walk, walked = _Walk(checkpoints, anchored=True), []
    for each in generations:
        try:
            handle = each.open("rb")
        except FileNotFoundError:
            continue
        with handle:
            stat = os.fstat(handle.fileno())
            walked.append(_identity(each, stat))
            broken = walk.file(each, handle, stat.st_size)
        if broken is not None:
            return walk, broken
    _VERIFIED[key] = (tuple(walked), copy.deepcopy(walk))
    return walk, None


def verify_ledger(
    audit_dir: Path | None = None, *, path: Path | None = None, count_task: str | None = None
) -> dict[str, Any]:
    """``VALID`` or ``BROKEN`` with the first break's file, line and reason.

    The audit dir's log: the live file is opened and sized, and the
    generations listed, under a shared flock (released before the walk;
    rows appended after it are past the snapshot) — a rotation
    only ever adds a generation, so a rotation mid-walk is never a false
    break. The chain must start at seq 1 from GENESIS (no generation is
    ever deleted), and every ``chain.sqlite3`` checkpoint must match its
    row and not lie past the head; a checkpoint that was never kept (its
    write failed) is counted in ``checkpoints_missing``, not a break.
    ``path``: one offline copy — its first chained row is its anchor
    (``starts_at``) unless older v1 rows precede it. ``count_task``: that
    run's rows, in the same snapshot as the head (``task_rows``). A missing
    ``path`` raises FileNotFoundError."""
    if path is not None:
        walk = _Walk({}, anchored=False)
        with path.open("rb") as offline:
            broken = walk.file(path, offline, os.fstat(offline.fileno()).st_size)
        return _result(walk, [path], None, broken, count_task)
    assert audit_dir is not None
    live = audit_dir / "policy" / "decisions.jsonl"
    checkpoints: dict[int, str] | str = {}
    handle: IO[bytes] | None = None
    size = 0
    with _shared_lock(live.parent / "decisions.lock"):
        generations = _rotated_generations(live)
        with suppress(FileNotFoundError):
            handle = live.open("rb")
            # Review R2 (F2): the size in the same snapshot as the checkpoints
            size = os.fstat(handle.fileno()).st_size
        try:
            checkpoints = _checkpoints(live.parent / CHAIN_DB)
        except sqlite3.DatabaseError as exc:
            checkpoints = f"{live.parent / CHAIN_DB} could not be read: {exc}"
    try:
        walk, broken = _generations(
            checkpoints if isinstance(checkpoints, dict) else {}, str(live), generations
        )
        if handle is not None and broken is None:
            broken = walk.file(live, handle, size)
    finally:
        if handle is not None:
            handle.close()
    if broken is None and isinstance(checkpoints, str):
        broken = {"file": None, "line": None, "reason": checkpoints}
    past = sorted(seq for seq in walk.checkpoints if walk.head is None or seq > walk.head["seq"])
    if broken is None and past:
        last = 0 if walk.head is None else walk.head["seq"]
        broken = {
            "file": None if walk.head is None else walk.head["file"],
            "line": None if walk.head is None else walk.head["line"],
            "reason": f"the log ends at seq {last} but {CHAIN_DB} holds a checkpoint "
            f"for seq {past[0]}: rows after seq {last} were removed",
        }
    return _result(walk, [*generations, live], checkpoints, broken, count_task)


def _result(
    walk: _Walk,
    files: list[Path],
    checkpoints: dict[int, str] | str | None,
    broken: dict[str, Any] | None,
    count_task: str | None,
) -> dict[str, Any]:
    """``checkpoints``: None for an offline copy (none are read)."""
    missing = 0
    if isinstance(checkpoints, dict) and walk.head is not None and walk.starts_at is not None:
        every = policy_v3.CHECKPOINT_EVERY
        due = range(-(-walk.starts_at // every) * every, walk.head["seq"] + 1, every)
        missing = sum(seq not in checkpoints for seq in due)
    return {
        "status": "BROKEN" if broken else "VALID",
        "files": [str(each) for each in files],
        "unchained": walk.unchained,
        "chained": walk.chained,
        "starts_at": walk.starts_at,
        "head": walk.head,
        "checkpoints": len(checkpoints) if isinstance(checkpoints, dict) else 0,
        "checkpoints_missing": missing,
        "task_rows": None if count_task is None else walk.tasks[count_task],
        "break": broken,
    }


def _patch(store: RunStore, task_id: str) -> dict[str, str]:
    """The patch's sha256 as ingest recorded it, and whether the file still hashes so."""
    recorded = next(
        ((p, sha) for kind, p, sha in store.artifacts_for(task_id) if kind == "patch"), None
    )
    if recorded is None:
        return {"sha256": "none recorded", "on_disk": "none"}
    try:
        actual = hashlib.sha256(Path(recorded[0]).read_bytes()).hexdigest()
    except FileNotFoundError:
        return {"sha256": recorded[1], "on_disk": "missing"}
    return {
        "sha256": recorded[1],
        "on_disk": "matches" if actual == recorded[1] else f"differs: now sha256 {actual}",
    }


def build_receipt(store: RunStore, home: Path, audit_dir: Path, task_id: str) -> dict[str, Any]:
    """One run's verifiable local receipt — ``skep proof export``. Built on
    the forensics report (redacted, ``unknown`` where no record holds a
    value), so it carries no secret and no guess. An unknown id raises
    ``require_run``'s 404."""
    from .forensics import UNKNOWN, _redacted, build_run_forensics

    report = build_run_forensics(store, audit_dir, task_id)
    ledger = dict(report["ledger"])  # Review R1 (Q6): head and count, one snapshot
    task_id, identity, g10 = report["task_id"], report["identity"], report["outcome"]["g10"]
    run, reverify = store.get_run(task_id), store.reverification_for(task_id)
    base = (run.base_commit if run else None) or (reverify.base_commit if reverify else None)
    pr = report["outcome"]["pr"]  # R4 (S5): the dispatch pin when G10 never ran
    if pr:
        # Phase 09 R1 (S14/N7): a PR verdict's baseline is the merge-base V1
        # counted on — the PR head is what was verified, never "baseline".
        base = pr.get("merge_base") or "merge-base unavailable"  # R2 (S9, N3)
    detail = next(
        (t["detail"] for t in report["outcome"]["transitions"] if t["state"] == "dispatched"),
        None,
    )
    try:
        dispatched = json.loads(detail or "{}")
    except ValueError:
        dispatched = {}
    dispatched = dispatched if isinstance(dispatched, dict) else {}
    baseline = baseline_status(home)
    counts = (g10 or {}).get("test_counts") or {}
    receipt = {
        "kind": "skep-local-receipt/1",
        "exported_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "skep_version": __version__,
        "task_id": task_id,
        "repo": identity["repo"],
        "baseline_sha": base or UNKNOWN,
        "pr": pr or None,
        "patch": _patch(store, task_id),
        "sandbox": {
            "execution_mode": identity["execution_mode"],
            "backend": identity["sandbox_backend"],
            # G3: only a walled worker records read_allowlist; G10 keeps its own wall
            "read_confinement": "strict"
            if "read_allowlist" in dispatched
            else "G10 only"
            if "g10_read_allowlist" in dispatched
            else "none recorded",
        },
        # ADR 0062: the dispatched line names the relay only when it was not off
        "key_relay": str(dispatched.get("provider_key_relay", "off")),
        "policy_baseline": {
            "state": baseline["state"],
            "sha256": baseline.get("sha256") or "none",
            "as_of": "export",
        },
        "taint": identity["taint"],
        "g10": None
        if g10 is None
        else {
            "outcome": g10["outcome"],
            "confirmed": g10["confirmed"],
            "commands": g10["commands"],
            "commands_source": g10["commands_source"],
            "v1": g10["v1"],
            "executed_tests": {
                side: (counts.get(side) or {}).get("total_executed", UNKNOWN)
                for side in ("baseline", "patched")
            },
        },
        "decisions": ledger.pop("task_rows"),
        "ledger": ledger,
        "scope": SCOPE,
    }
    scrubbed: dict[str, Any] = _redacted(receipt, redact_export)  # Review R1 (Q3)
    return scrubbed


# Review R1 (Q4, I2/I8): where G10's command came from, said as the record says it.
_SOURCE = {
    "project": "from the project pin",
    "worker": "from the worker's own verify step (unpinned fallback)",
}


def _code(text: str) -> str:
    """A Markdown code span no backtick inside ``text`` can close (Q7)."""
    fence = "`" * (max(map(len, re.findall("`+", text)), default=0) + 1)
    return f"{fence}{text}{fence}" if fence == "`" else f"{fence} {text} {fence}"


def _plain(text: object) -> str:
    return str(text).replace("`", "\\`")


_CELL = str.maketrans({"|": "\\|", "\n": " ", "\r": " "})  # Q7: a value never reshapes the table


def broken_at(where: dict[str, Any]) -> str:
    """A break's place — ``-`` where the reason names no file or line (Q7)."""
    return f"BROKEN at {where.get('file') or '-'}:{where.get('line') or '-'}: {where['reason']}"


def render_receipt(receipt: dict[str, Any], fmt: str) -> str:
    """``json`` (sorted, indented) or ``md``: one collapsible block to paste into a PR."""
    from .forensics import pr_line

    if fmt == "json":
        return json.dumps(receipt, indent=2, sort_keys=True)
    g10, ledger, sandbox, patch, baseline = (
        receipt[k] for k in ("g10", "ledger", "sandbox", "patch", "policy_baseline")
    )
    if ledger["break"]:
        chain = _plain(broken_at(ledger["break"]))
    elif ledger["head_hash"]:
        chain = f"VALID, head seq {ledger['head_seq']} sha256 {_code(ledger['head_hash'])}"
    else:
        chain = "VALID, no chained rows yet"
    if ledger["unchained_rows"]:
        chain += f"; {ledger['unchained_rows']} older v1 rows are unchained"
    if ledger["checkpoints_missing"]:
        chain += f"; {ledger['checkpoints_missing']} checkpoints were never kept"
    verdict = "G10 not run" if g10 is None else f"G10 {g10['outcome']}, V1 {g10['v1']}"
    rows = [
        ("repo", _plain(receipt["repo"])),
        *([("PR", _plain(pr_line(receipt["pr"])))] if receipt.get("pr") else []),
        ("baseline (PR merge-base)" if receipt.get("pr") else "baseline",
         _code(receipt["baseline_sha"])),
        ("patch sha256", f"{_code(patch['sha256'])} (on disk: {_plain(patch['on_disk'])})"),
        ("skep", _plain(receipt["skep_version"])),
        ("sandbox", _plain(f"{sandbox['execution_mode']}, backend {sandbox['backend']}, "
                           f"reads {sandbox['read_confinement']}")),
        ("key relay", _plain(receipt["key_relay"])),
        ("policy baseline (at export)",
         _plain(baseline["state"])
            + ("" if baseline["sha256"] == "none" else f" {_code(baseline['sha256'])}")),
        ("taint", _plain(receipt["taint"])),
    ]  # fmt: skip
    if g10 is not None:
        executed = g10["executed_tests"]
        source = _SOURCE.get(g10["commands_source"], "source unknown")
        rows += [
            ("G10", f"{_plain(g10['outcome'])} (confirmed={g10['confirmed']}), "
             f"{_code(' && '.join(g10['commands']))} {source}"),
            ("V1", _plain(g10["v1"])),
            ("executed tests",
             f"{executed['baseline']} on the baseline, {executed['patched']} patched"),
        ]  # fmt: skip
    rows += [
        ("decisions for this run", str(receipt["decisions"])),
        ("ledger", chain),
        ("exported", receipt["exported_at"]),
    ]
    return "\n".join(
        [
            "<details>",
            f"<summary>skep verifiable local receipt · {receipt['task_id']} · {verdict} · "
            f"ledger {ledger['status']}</summary>",
            "",
            "| | |",
            "|---|---|",
            *(f"| {key} | {value.translate(_CELL)} |" for key, value in rows),
            "",
            f"> {receipt['scope']}",
            "",
            "</details>",
        ]
    )
