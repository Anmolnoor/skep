"""UUIDv7 minting (decision Q7): supervisor owns the ID namespace end-to-end."""

from __future__ import annotations

import secrets
import time
import uuid


def mint_uuid7() -> str:
    """Return a time-sortable UUIDv7 string (RFC 9562 layout)."""
    timestamp_ms = time.time_ns() // 1_000_000
    rand_a = secrets.randbits(12)
    rand_b = secrets.randbits(62)
    value = (timestamp_ms & ((1 << 48) - 1)) << 80 | 0x7 << 76 | rand_a << 64 | 0b10 << 62 | rand_b
    return str(uuid.UUID(int=value))
