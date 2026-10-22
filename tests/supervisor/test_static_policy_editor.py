"""P2 (ADR 0053): the registry-driven policy editor on the Policies page.

The UI is a no-build static app (I11), so the pins are text checks over
app.js in the `test_style_tokens` idiom — the tier exists, it reads the
registry and the RSoP, it asks `why`, and it writes through the routes that
already exist (I5) — plus the one route change the editor needed: the
project upsert keeps the pack it was given.
"""

from __future__ import annotations

from pathlib import Path

from skep.supervisor import SupervisorConfig

from .conftest import serve_client

APP_JS = Path(__file__).resolve().parents[2] / "src/skep/supervisor/serve/static/app.js"


def test_the_editor_tier_reads_the_registry_and_writes_through_existing_routes() -> None:
    text = APP_JS.read_text()
    assert 'key: "resolve", label: "Resolve & edit"' in text
    assert 'api("GET", "/api/policy/registry")' in text
    assert "/rsop`" in text
    assert "/api/policy/why?repo=" in text
    # Writes: the project overlay is the definition re-POSTed; globals PUT.
    assert 'api("POST", "/api/projects", { ...project, policy: nextPolicy })' in text
    assert 'api("PUT", "/api/policy", body)' in text
    assert 'api("PUT", "/api/policy", { policy_engine_v2_scopes: scopes })' in text
    # One typed control per registry type, nothing hand-listed per key.
    assert "function knobInput(knob, current)" in text
    for widget in (
        'type === "bool"',
        'type === "int"',
        'type.startsWith("enum[")',
        'type === "list[argv]"',
        'type === "list[str]"',
    ):
        assert widget in text, widget
    # Tri-state as it exists today: inherit (absent) or set; cleared is named
    # as not-yet-storable, never faked (I8).
    assert '"cleared"' in text and "has no stored form yet" in text


def test_the_project_upsert_keeps_the_pack_it_was_given(
    config: SupervisorConfig, repo: Path
) -> None:
    """The editor GETs a definition and POSTs it back with a new overlay; a
    pack pinned by setup must survive that round trip."""
    client = serve_client(config)
    body = {
        "project_id": "app",
        "name": "app",
        "strategy": "trusted_local_dev",
        "phase": "build",
        "policy": {"default_max_actions": 5},
        "bindings": [{"kind": "repo_path", "value": str(repo)}],
        "pack_name": "trusted_local_dev",
        "pack_version": "1",
    }
    created = client.post("/api/projects", json=body)
    assert created.status_code == 201, created.text
    assert created.json()["pack_name"] == "trusted_local_dev"
    fetched = client.get("/api/projects/app").json()
    assert fetched["pack_name"] == "trusted_local_dev" and fetched["pack_version"] == "1"
    edited = client.post("/api/projects", json={**fetched, "policy": {"default_max_actions": 9}})
    assert edited.status_code == 201, edited.text
    again = client.get("/api/projects/app").json()
    assert again["policy"] == {"default_max_actions": 9}
    assert again["pack_name"] == "trusted_local_dev" and again["pack_version"] == "1"
    assert again["bindings"] == [{"kind": "repo_path", "value": str(repo)}]
