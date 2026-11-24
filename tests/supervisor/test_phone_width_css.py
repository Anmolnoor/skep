"""Phase 10 R2 I9: at a phone width (<= 640px, the 390px iPhone the browser
proof used) a run page and the New run form must not scroll sideways. The
browser measured it; this pins the CSS rules that make it so."""

from __future__ import annotations

import re

from skep.supervisor.serve.app import STATIC_DIR

CSS = (STATIC_DIR / "style.css").read_text()


def _block(at: str) -> str:
    """The body of the first ``@media`` block whose prelude is ``at``."""
    start = CSS.index(at)
    open_at = CSS.index("{", start)
    depth, i = 0, open_at
    while True:
        depth += {"{": 1, "}": -1}.get(CSS[i], 0)
        if depth == 0:
            return CSS[open_at + 1 : i]
        i += 1


def _rule(css: str, selector: str) -> str:
    found = re.findall(rf"(?m)^\s*{re.escape(selector)}\s*\{{([^}}]*)\}}", css)
    assert found, f"no rule for {selector!r}"
    return " ".join(found)


PHONE = _block("@media (max-width: 640px)")
TOP = CSS.replace(PHONE, "")


def test_run_metadata_cells_wrap_their_paths() -> None:
    assert "minmax(0, 1fr)" in _rule(TOP, ".kv")
    cells = _rule(TOP, ".kv dt, .kv dd")
    assert "min-width: 0" in cells and "overflow-wrap: anywhere" in cells
    assert "grid-template-columns: minmax(0, 1fr)" in _rule(PHONE, ".kv")


def test_the_timeline_scrolls_inside_its_card() -> None:
    timeline = _rule(PHONE, ".run-timeline")
    assert "overflow-x: auto" in timeline and "max-width: 100%" in timeline
    assert "min-width: 0" in timeline


def test_the_new_run_form_fits_a_phone() -> None:
    assert "flex-wrap: nowrap" in _rule(PHONE, ".composer.stacky")
    assert "min-width: 0" in _rule(PHONE, ".composer.stacky .field, .composer.stacky .field.grow")
    assert "max-width: 100%" in _rule(PHONE, "input, textarea, select")


def test_decision_filters_stay_a_wrapping_row_on_a_phone() -> None:
    """N4: the phone block flips .row to a column; the filters' rule must
    outrank it (two classes beat one), or flex-basis 9rem becomes a height."""
    assert "flex-direction: column" in _rule(PHONE, ".row")
    filters = _rule(TOP, ".row.decision-filters")
    assert "flex-direction: row" in filters and "flex-wrap: wrap" in filters
