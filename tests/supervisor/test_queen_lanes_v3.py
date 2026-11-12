"""ADR 0059 P3-2: the Queen's file and shell lanes decide through
``policy_v3.decide()``. Nothing they decide is more permissive than before,
except a human-approved learned grant answering a catch-all ask."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from skep.supervisor.policy_schema import _VERDICT_RANK

from .queen_lane_grid import HOMES, rows

FROZEN: dict[str, str] = json.loads(
    (
        Path(__file__).parents[1] / "fixtures" / "policy_v3" / "pre_p3_2_queen_verdicts.json"
    ).read_text()
)
_RANK = {**_VERDICT_RANK, "card": _VERDICT_RANK["require_approval"]}


def _flips(kind: str, root: Path) -> Counter[str]:
    """Every cell of one home against its frozen pre-P3-2 answer, classified."""
    seen: Counter[str] = Counter()
    for key, shown in rows(root, kind):
        old_verdict, old_by = FROZEN[key].split(" ", 1)
        new_verdict, new_by = shown.split(" ", 1)
        assert "error" not in (old_verdict, new_verdict), (key, FROZEN[key], shown)
        old, new = _RANK[old_verdict], _RANK[new_verdict]
        if new < old:
            seen["looser"] += 1
            raise AssertionError(f"more permissive: {key}: {FROZEN[key]} -> {shown}")
        if new > old:
            seen["stricter"] += 1
        elif old_by != new_by:
            seen["label"] += 1
        if old_by.startswith("floor/"):
            assert new_by == old_by, (key, FROZEN[key], shown)
    return seen


@pytest.mark.parametrize("kind", HOMES)
def test_no_queen_lane_verdict_is_more_permissive_than_before(
    kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The property: home x role x grant x the file and shell probes, held to
    what the lanes decided before P3-2 (frozen). A floor denial stays the
    same floor denial."""
    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    monkeypatch.chdir(tmp_path)
    _flips(kind, tmp_path)
