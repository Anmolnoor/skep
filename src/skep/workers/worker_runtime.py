"""Shared runtime helpers for first-party workers."""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from uuid import uuid4

from skep.worker_contract import CONTRACT_VERSION, CodingWorkerResult, Event, EventType


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def manifest_fingerprint(
    worker_version: str,
    worker_caste: str,
    runtime_manifest: object | None = None,
) -> str:
    base = f"{worker_version}:{worker_caste}"
    if runtime_manifest is not None:
        manifest = json.dumps(runtime_manifest, sort_keys=True)
        base = f"{base}:{manifest}"
    return hashlib.sha256(base.encode()).hexdigest()


class EventStream:
    def __init__(self, path: Path, *, task_id: str, trace_id: str) -> None:
        self.path = path
        self._task_id = task_id
        self._trace_id = trace_id
        self._seq = 0
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, event_type: EventType, payload: dict[str, object]) -> None:
        with self._lock:
            self._seq += 1
            event = Event(
                contract_version=CONTRACT_VERSION,
                event_id=str(uuid4()),
                seq=self._seq,
                task_id=self._task_id,
                trace_id=self._trace_id,
                ts=utc_now(),
                type=event_type,
                payload=payload,
            )
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(event.model_dump_json() + "\n")


class Heartbeat:
    def __init__(
        self,
        stream: EventStream,
        phase: str,
        *,
        interval_seconds: float = 5.0,
        emit_immediately: bool = True,
    ) -> None:
        self._stream = stream
        self._phase = phase
        self._interval_seconds = interval_seconds
        self._emit_immediately = emit_immediately
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> Heartbeat:
        if self._emit_immediately:
            self._emit()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def _emit(self) -> None:
        self._stream.emit(EventType.HEARTBEAT, {"phase": self._phase})

    def _loop(self) -> None:
        while not self._stop.wait(self._interval_seconds):
            self._emit()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)


def write_result(out_path: Path, result: CodingWorkerResult) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(result.model_dump_json(indent=2) + "\n", encoding="utf-8")
