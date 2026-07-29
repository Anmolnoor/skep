from __future__ import annotations


def route(path: str) -> tuple[int, dict[str, str]]:
    if path == "/":
        return 200, {"message": "hello from skep-demo"}
    return 404, {"error": "not found"}
