from __future__ import annotations

from skep.workers import audit, coding_minimal


def test_coding_and_audit_workers_share_runtime_helpers() -> None:
    from skep.workers.worker_runtime import EventStream, Heartbeat, sha256_file, write_result

    shared = {
        "_EventStream": EventStream,
        "_Heartbeat": Heartbeat,
        "_sha256_file": sha256_file,
        "_write_result": write_result,
    }
    for module in (coding_minimal, audit):
        for name, helper in shared.items():
            assert vars(module)[name] is helper, f"{module.__name__}.{name} diverged"
