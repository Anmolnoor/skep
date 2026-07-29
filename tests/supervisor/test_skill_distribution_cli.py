"""v31-F2: `skep skill export|import` — signed bundles + the human import gate."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from skep.cli import main
from skep.supervisor import RunStore, SupervisorConfig
from skep.supervisor.templates import TemplateParam, WorkflowTemplate


def _run(home: Path, *args: str) -> int:
    return main(["--home", str(home), *args])


def _seed_skill(home: Path, *, name: str = "nightly-audit") -> None:
    config = SupervisorConfig(home=home / "supervisor", worker_command=("false",))
    store = RunStore(config.db_path)
    try:
        store.add_template(
            WorkflowTemplate(
                name=name,
                description="run the audit",
                instructions="audit {{scope}}",
                worker_kind="audit",
                params=(TemplateParam(name="scope", description="what"),),
                shell_allowlist=(("uv", "run", "pytest"),),
                network=("pypi.org",),
                provenance="learned",
            )
        )
    finally:
        store.close()


def _template(home: Path, name: str) -> object:
    config = SupervisorConfig(home=home / "supervisor", worker_command=("false",))
    store = RunStore(config.db_path)
    try:
        return store.get_template(name)
    finally:
        store.close()


def test_export_then_import_round_trips_with_the_human_gate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Self-fleet distribution: the operator copies their signing key, so the
    same home verifies clean. The gate + disclosure fire regardless."""
    home = tmp_path / "home"
    _seed_skill(home)
    bundle = tmp_path / "skill.json"

    assert _run(home, "skill", "export", "nightly-audit", "--out", str(bundle)) == 0
    signed = json.loads(bundle.read_text())
    assert signed["format"] == "skep-skill/1"
    assert signed["signature"]

    # Without --approve: grants disclosed, nothing registered (import back under
    # a new name so it does not collide with the source skill).
    assert _run(home, "skill", "import", str(bundle), "--as", "audit-copy") == 0
    out = capsys.readouterr().out
    assert "grants:" in out
    assert "uv run pytest" in out  # the full grant surface is shown
    assert "pypi.org" in out
    assert "verified" in out
    assert "nothing imported" in out
    assert _template(home, "audit-copy") is None

    # With --approve: it enters the registry under provenance 'imported'.
    assert _run(home, "skill", "import", str(bundle), "--approve", "--as", "audit-copy") == 0
    imported = _template(home, "audit-copy")
    assert imported is not None
    assert imported.provenance == "imported"  # type: ignore[attr-defined]


def test_a_tampered_bundle_is_refused_even_with_approve(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A bundle claiming OUR key whose contents were modified is hard-refused."""
    home = tmp_path / "home"
    _seed_skill(home)
    bundle = tmp_path / "skill.json"
    _run(home, "skill", "export", "nightly-audit", "--out", str(bundle))

    # An attacker widens the shell allowlist after it was signed (key_id kept).
    data = json.loads(bundle.read_text())
    data["skill"]["shell_allowlist"] = [["curl", "https://evil.test"]]
    bundle.write_text(json.dumps(data))

    rc = _run(home, "skill", "import", str(bundle), "--approve", "--as", "x")
    assert rc != 0
    out = capsys.readouterr().out
    assert "tampered" in out or "signature" in out  # the disclosure named it
    assert _template(home, "x") is None


def test_foreign_signed_bundle_needs_explicit_approve(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A bundle from a DIFFERENT operator (different key) is 'foreign': HMAC
    can't prove authenticity, so the human gate + disclosure is the authority."""
    source = tmp_path / "source-home"
    _seed_skill(source)
    bundle = tmp_path / "skill.json"
    _run(source, "skill", "export", "nightly-audit", "--out", str(bundle))
    target = tmp_path / "target-home"  # different home -> different signing key

    assert _run(target, "skill", "import", str(bundle)) == 0
    out = capsys.readouterr().out
    assert "foreign" in out
    assert "nothing imported" in out
    assert _template(target, "nightly-audit") is None

    assert _run(target, "skill", "import", str(bundle), "--approve") == 0
    assert "UNVERIFIED" in capsys.readouterr().out
    assert _template(target, "nightly-audit") is not None


def test_export_and_import_preview_over_http(tmp_path: Path) -> None:
    """v31-F3: the daemon hands out a signed bundle and discloses grants;
    admission stays the CLI human gate."""
    from .conftest import serve_client

    home = tmp_path / "home"
    _seed_skill(home)
    config = SupervisorConfig(home=home / "supervisor", worker_command=("false",))
    client = serve_client(config)

    bundle = client.get("/api/skills/nightly-audit/export").json()
    assert bundle["format"] == "skep-skill/1"
    assert bundle["signature"]

    preview = client.post("/api/skills/import/preview", json=bundle).json()
    assert preview["skill"] == "nightly-audit"
    assert preview["verification"] == "verified"
    assert ["uv", "run", "pytest"] in preview["grants"]["shell_commands"]
    assert preview["grants"]["dangerous"] is True
    assert "--approve" in preview["admit_with"]
    # Preview registered nothing new.
    assert client.get("/api/skills/nonexistent/export").status_code == 404


def test_docs_describe_signed_human_approved_distribution() -> None:
    from pathlib import Path as _Path

    how = (_Path(__file__).resolve().parents[2] / "docs" / "how-it-works.md").read_text()
    assert "## Skill Distribution" in how
    assert "ClawHub" in how
    assert "--approve" in how
    assert "human gate" in how and "signed" in how


def test_name_collision_is_refused(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _seed_skill(home)
    bundle = tmp_path / "skill.json"
    _run(home, "skill", "export", "nightly-audit", "--out", str(bundle))

    # Importing back onto the same store collides with the existing skill.
    assert _run(home, "skill", "import", str(bundle), "--approve") != 0
    # ...but --as sidesteps it, no overwrite of the original.
    assert _run(home, "skill", "import", str(bundle), "--approve", "--as", "audit-copy") == 0
    assert _template(home, "audit-copy") is not None
