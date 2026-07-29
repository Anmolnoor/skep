"""v17 Step 4: the deep research template."""

from __future__ import annotations

from skep.supervisor.templates import (
    deep_research_template,
    instantiate,
    validate_template,
)


def test_template_validates_and_has_research_params() -> None:
    template = deep_research_template(("docs.python.org", "pypi.org"))
    validate_template(template)
    assert template.worker_kind == "researcher"
    assert {p.name for p in template.params} == {
        "question",
        "depth",
        "output_format",
        "sources",
    }
    # The source allowlist IS the run's D1 network allowlist (egress pinned).
    assert template.network == ("docs.python.org", "pypi.org")


def test_instantiate_substitutes_params_and_carries_allowlist() -> None:
    template = deep_research_template(("docs.python.org",))
    instance = instantiate(
        template,
        {
            "question": "how does asyncio work",
            "depth": "deep",
            "output_format": "html",
            "sources": "https://docs.python.org/3/library/asyncio-task.html",
        },
        repo="/tmp/x",
    )
    assert "how does asyncio work" in instance.instructions
    assert "deep" in instance.instructions
    # v46-F1: the discovered article URLs ride the instructions.
    assert "Sources: https://docs.python.org/3/library/asyncio-task.html" in instance.instructions
    assert instance.worker_kind == "researcher"
    # The instantiated run keeps the source allowlist as its network permission.
    assert instance.permissions.network == ["docs.python.org"]
    # Pre-v46 callers omit sources entirely and still instantiate (empty line).
    legacy = instantiate(template, {"question": "q"}, repo="/tmp/x")
    assert "Sources: \n" in legacy.instructions


def test_depth_and_format_have_safe_defaults() -> None:
    template = deep_research_template(("pypi.org",))
    depth = next(p for p in template.params if p.name == "depth")
    fmt = next(p for p in template.params if p.name == "output_format")
    assert depth.default == "standard"
    assert fmt.default == "markdown"
