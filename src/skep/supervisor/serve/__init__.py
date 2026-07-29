"""skep serve (v5): the HTTP API daemon over the supervisor core."""

from __future__ import annotations

from importlib import import_module
from typing import Any

__all__ = ["DispatchError", "Dispatcher", "create_app"]


def __getattr__(name: str) -> Any:
    if name == "create_app":
        return import_module(".app", __name__).create_app
    if name in {"Dispatcher", "DispatchError"}:
        jobs = import_module(".jobs", __name__)
        return getattr(jobs, name)
    raise AttributeError(name)
