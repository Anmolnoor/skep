"""ES-F9 (B43): release N callables at the same instant.

No test raced anything inside one test, so every check-then-act window in
the ledger (B13 to B17) passed review and the suite alike. ``race`` starts
one thread per callable, holds them all at a ``threading.Barrier`` and
lets them go together; it returns, in call order, each callable's result
or the exception it raised — a test asserts on the outcomes, it never has
to catch inside a thread.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any


def race(*calls: Callable[[], Any], timeout: float = 30.0) -> list[Any]:
    """Run every call at once; a result or the raised exception per call."""
    barrier = threading.Barrier(len(calls))

    def released(call: Callable[[], Any]) -> Any:
        barrier.wait(timeout)
        return call()

    with ThreadPoolExecutor(max_workers=len(calls)) as pool:
        futures = [pool.submit(released, call) for call in calls]
        return [future.exception(timeout) or future.result() for future in futures]
