"""Suite-wide fixtures."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from tests.supervisor import strict_wire


@pytest.fixture(autouse=True)
def _strict_fake_refusals() -> Iterator[None]:
    """ES-F1 (B43): a strict provider fake's 400 fails the test even when the
    product swallowed it (halve-on-4xx, retry). Tests that provoke one on
    purpose assert on ``strict_wire.REJECTIONS`` and clear it."""
    strict_wire.REJECTIONS.clear()
    yield
    refused = list(strict_wire.REJECTIONS)
    strict_wire.REJECTIONS.clear()
    assert not refused, f"a strict provider fake refused a request body: {refused}"
