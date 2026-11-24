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

import fcntl
import hashlib
import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import ExitStack, closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

from .. import __version__
from .policy_baseline import baseline_status
from .policy_v3 import CHAIN_DB, GENESIS, _rotated_generations, chain_hash
from .store import RunStore

SCOPE = (
    "Local receipt, not tamper-proof: the hash chain shows the decision log was not "
    "edited, reordered or cut in the middle since it was written, and the head hash pins "
    "it as of export. Whoever can write the skep home can rewrite the whole log and its "
    "checkpoints, so the anchor outside this machine is this receipt where you publish it "
    "(a PR description or a commit)."
)


def _lines(handle: IO[bytes], size: int) -> Iterator[str]:
    """The file's lines up to ``size`` bytes — its length at the snapshot."""
    read = 0
    for raw in handle:
        if read >= size:
            return
        read += len(raw)
        yield raw.decode("utf-8", errors="replace").removesuffix("\n")


def _checkpoints(db: Path) -> dict[int, str]:
    if not db.exists():
        return {}
    with closing(sqlite3.connect(f"file:{db}?mode=ro", uri=True)) as conn:
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


def verify_ledger(audit_dir: Path | None = None, *, path: Path | None = None) -> dict[str, Any]:
    """``VALID`` or ``BROKEN`` with the first break's file, line and reason.

    The audit dir's log: every generation plus the live file, opened as one
    snapshot under a shared flock (released before the walk), so a rotation
    mid-walk is never a false break. The chain must start at seq 1 from
    GENESIS (no generation is ever deleted), and every ``chain.sqlite3``
    checkpoint must match its row and not lie past the head. ``path``: one
    offline copy — its first chained row is its anchor (``starts_at``), as
    the rows before it live in some other file. A missing ``path`` raises
    FileNotFoundError."""
    files: list[tuple[Path, IO[bytes], int]] = []
    checkpoints: dict[int, str] | str = {}
    with ExitStack() as stack:

        def snap(each: Path) -> None:
            handle = stack.enter_context(each.open("rb"))
            files.append((each, handle, os.fstat(handle.fileno()).st_size))

        if path is not None:
            snap(path)
        else:
            assert audit_dir is not None
            live = audit_dir / "policy" / "decisions.jsonl"
            with _shared_lock(live.parent / "decisions.lock"):
                for each in (*_rotated_generations(live), live):
                    try:
                        snap(each)
                    except FileNotFoundError:
                        continue
                try:
                    checkpoints = _checkpoints(live.parent / CHAIN_DB)
                except sqlite3.DatabaseError as exc:
                    checkpoints = f"{live.parent / CHAIN_DB} could not be read: {exc}"
        return _walk(files, checkpoints, anchored=path is None)


def _walk(
    files: list[tuple[Path, IO[bytes], int]], checkpoints: dict[int, str] | str, *, anchored: bool
) -> dict[str, Any]:
    head: dict[str, Any] | None = None
    unchained = chained = 0
    starts_at = None

    def result(broken: dict[str, Any] | None) -> dict[str, Any]:
        return {
            "status": "BROKEN" if broken else "VALID",
            "files": [str(each) for each, _, _ in files],
            "unchained": unchained,
            "chained": chained,
            "starts_at": starts_at,
            "head": head,
            "checkpoints": 0 if isinstance(checkpoints, str) else len(checkpoints),
            "break": broken,
        }

    def broken(where: dict[str, Any], reason: str) -> dict[str, Any]:
        return result({**where, "reason": reason})

    for path, handle, size in files:
        for line, text in enumerate(_lines(handle, size), 1):
            where = {"file": str(path), "line": line}
            try:
                row = json.loads(text)
            except ValueError:
                row = None
            if not isinstance(row, dict) or row.get("v") != 2:
                if head is not None:
                    return broken(where, "an unchained line (v1 or unreadable) inside the chain")
                unchained += 1  # before the chain: an old v1 row, never rewritten
                continue
            if json.dumps(row) != text:
                return broken(
                    where, "the line is not the bytes skep wrote (it re-serialises differently)"
                )
            stored, seq = row.pop("hash", None), row.get("seq")
            if type(seq) is not int:
                return broken(where, "the row has no integer seq")
            if stored != chain_hash(row):
                return broken(where, f"seq {seq}'s hash does not match its content")
            if head is None:
                if anchored and (seq != 1 or row.get("prev_hash") != GENESIS):
                    return broken(
                        where,
                        f"the chain starts at seq {seq}, not at seq 1 from GENESIS: "
                        "earlier rows (or a whole generation) are missing",
                    )
                starts_at = seq
            elif seq != head["seq"] + 1:
                return broken(
                    where, f"seq {seq} follows seq {head['seq']}: a row is missing or moved"
                )
            elif row.get("prev_hash") != head["hash"]:
                return broken(where, f"seq {seq}'s prev_hash is not seq {head['seq']}'s hash")
            if isinstance(checkpoints, dict) and checkpoints.get(seq, stored) != stored:
                return broken(where, f"seq {seq} differs from its checkpoint in {CHAIN_DB}")
            head = {"seq": seq, "hash": stored, "file": str(path), "line": line}
            chained += 1
    if isinstance(checkpoints, str):
        return result({"file": None, "line": None, "reason": checkpoints})
    past = sorted(seq for seq in checkpoints if head is None or seq > head["seq"])
    if past:
        last = 0 if head is None else head["seq"]
        return result(
            {
                "file": None if head is None else head["file"],
                "line": None if head is None else head["line"],
                "reason": f"the log ends at seq {last} but {CHAIN_DB} holds a checkpoint "
                f"for seq {past[0]}: rows after seq {last} were removed",
            }
        )
    return result(None)


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
    from .forensics import UNKNOWN, build_run_forensics

    report = build_run_forensics(store, audit_dir, task_id)
    task_id, identity, g10 = report["task_id"], report["identity"], report["outcome"]["g10"]
    run, reverify = store.get_run(task_id), store.reverification_for(task_id)
    base = (run.base_commit if run else None) or (reverify.base_commit if reverify else None)
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
    return {
        "kind": "skep-local-receipt/1",
        "exported_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "skep_version": __version__,
        "task_id": task_id,
        "repo": identity["repo"],
        "baseline_sha": base or UNKNOWN,
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
        "decisions": sum(row.get("task_id") == task_id for row in report["policy_trail"]),
        "ledger": report["ledger"],
        "scope": SCOPE,
    }


def render_receipt(receipt: dict[str, Any], fmt: str) -> str:
    """``json`` (sorted, indented) or ``md``: one collapsible block to paste into a PR."""
    if fmt == "json":
        return json.dumps(receipt, indent=2, sort_keys=True)
    g10, ledger, sandbox, patch, baseline = (
        receipt[k] for k in ("g10", "ledger", "sandbox", "patch", "policy_baseline")
    )
    if ledger["break"]:
        where = ledger["break"]
        chain = f"BROKEN at {where['file']}:{where['line']}: {where['reason']}"
    elif ledger["head_hash"]:
        chain = f"VALID, head seq {ledger['head_seq']} sha256 `{ledger['head_hash']}`"
    else:
        chain = "VALID, no chained rows yet"
    verdict = "G10 not run" if g10 is None else f"G10 {g10['outcome']}, V1 {g10['v1']}"
    rows = [
        ("repo", receipt["repo"]),
        ("baseline", f"`{receipt['baseline_sha']}`"),
        ("patch sha256", f"`{patch['sha256']}` (on disk: {patch['on_disk']})"),
        ("skep", receipt["skep_version"]),
        ("sandbox", f"{sandbox['execution_mode']}, backend {sandbox['backend']}, "
         f"reads {sandbox['read_confinement']}"),
        ("key relay", receipt["key_relay"]),
        ("policy baseline (at export)",
         receipt["policy_baseline"]["state"]
            + ("" if baseline["sha256"] == "none" else f" `{baseline['sha256']}`")),
        ("taint", receipt["taint"]),
    ]  # fmt: skip
    if g10 is not None:
        executed = g10["executed_tests"]
        rows += [
            ("G10", f"{g10['outcome']} (confirmed={g10['confirmed']}), "
             f"`{' && '.join(g10['commands'])}` from the {g10['commands_source']} pin"),
            ("V1", g10["v1"]),
            ("executed tests",
             f"{executed['baseline']} on the baseline, {executed['patched']} patched"),
        ]  # fmt: skip
    rows += [
        ("decisions for this run", str(receipt["decisions"])),
        ("ledger", chain + (f"; {ledger['unchained_rows']} older v1 rows are unchained"
                            if ledger["unchained_rows"] else "")),
        ("exported", receipt["exported_at"]),
    ]  # fmt: skip
    return "\n".join(
        [
            "<details>",
            f"<summary>skep verifiable local receipt · {receipt['task_id']} · {verdict} · "
            f"ledger {ledger['status']}</summary>",
            "",
            "| | |",
            "|---|---|",
            *(f"| {key} | {value} |" for key, value in rows),
            "",
            f"> {receipt['scope']}",
            "",
            "</details>",
        ]
    )
