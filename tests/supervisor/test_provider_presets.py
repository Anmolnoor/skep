"""v108-F3: the preset catalog — Hermes parity as data.

Every row must build a profile the registry's own validator accepts; the
rows that cannot (azure's per-resource endpoint) must say so instead of
guessing. Bedrock's control-plane host follows the chosen region — explicit
per-region rows, never a wildcard (I12)."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from skep.supervisor import SupervisorConfig
from skep.supervisor.cli_cmds import cmd_provider_add, cmd_provider_presets
from skep.supervisor.provider_presets import (
    PROVIDER_PRESETS,
    preset_egress_note,
    preset_view,
    profile_from_preset,
)
from skep.supervisor.providers import ProviderError, validate_provider_profile

from .conftest import serve_client


def test_every_preset_builds_a_valid_profile() -> None:
    for preset_id, preset in PROVIDER_PRESETS.items():
        base_url = preset.base_url or "https://resource.example.com/v1"
        profile = profile_from_preset(preset_id, base_url=base_url)
        validated = validate_provider_profile(profile)
        assert validated.source == f"preset:{preset_id}"
        assert validated.protocol == preset.protocol
        # The endpoint host is always an explicit allowlist entry.
        assert validated.allowed_network_hosts


def test_unknown_preset_and_missing_base_url_refuse_loudly() -> None:
    with pytest.raises(ProviderError) as err:
        profile_from_preset("hermes")
    assert "unknown preset" in str(err.value)
    with pytest.raises(ProviderError) as err:
        profile_from_preset("azure-foundry")
    assert "base_url" in str(err.value)


def test_bedrock_control_plane_host_follows_the_region() -> None:
    default = validate_provider_profile(profile_from_preset("bedrock"))
    assert "bedrock-runtime.us-east-1.amazonaws.com" in default.allowed_network_hosts
    assert "bedrock.us-east-1.amazonaws.com" in default.allowed_network_hosts

    eu = validate_provider_profile(
        profile_from_preset("bedrock", base_url="https://bedrock-runtime.eu-west-1.amazonaws.com")
    )
    assert "bedrock-runtime.eu-west-1.amazonaws.com" in eu.allowed_network_hosts
    assert "bedrock.eu-west-1.amazonaws.com" in eu.allowed_network_hosts
    assert "bedrock.us-east-1.amazonaws.com" not in eu.allowed_network_hosts


def test_copilot_preset_carries_the_exchange_host() -> None:
    profile = validate_provider_profile(profile_from_preset("github-copilot"))
    assert "api.githubcopilot.com" in profile.allowed_network_hosts
    assert "api.github.com" in profile.allowed_network_hosts


def test_egress_notes_tell_the_truth() -> None:
    lmstudio = PROVIDER_PRESETS["lmstudio"]
    assert "nothing leaves this machine" in preset_egress_note(lmstudio, lmstudio.base_url or "")
    openrouter = PROVIDER_PRESETS["openrouter"]
    note = preset_egress_note(openrouter, openrouter.base_url or "")
    assert note.startswith("CLOUD") and "openrouter.ai" in note
    # No preset ships a key value or an OAuth client id — NAMES only.
    for view in map(preset_view, PROVIDER_PRESETS.values()):
        key_env = view["api_key_env"]
        assert key_env is None or str(key_env).isidentifier()


def test_rest_preset_flow(config: SupervisorConfig) -> None:
    client = serve_client(config)
    catalog = client.get("/api/provider-presets").json()["presets"]
    assert {row["preset_id"] for row in catalog} == set(PROVIDER_PRESETS)

    created = client.post("/api/providers", json={"preset": "openrouter"})
    assert created.status_code == 201
    provider = created.json()["provider"]
    assert provider["provider_id"] == "openrouter"
    assert provider["source"] == "preset:openrouter"
    assert provider["api_key_env"] == "OPENROUTER_API_KEY"
    assert "openrouter.ai" in created.json()["egress"]

    # Overrides win over the preset row.
    custom = client.post(
        "/api/providers", json={"preset": "deepseek", "provider_id": "ds", "model": "deepseek-r2"}
    )
    assert custom.status_code == 201
    assert custom.json()["provider"]["provider_id"] == "ds"
    assert custom.json()["provider"]["model"] == "deepseek-r2"

    bad = client.post("/api/providers", json={"preset": "azure-foundry"})
    assert bad.status_code == 400
    assert "base_url" in bad.json()["detail"]


def test_cli_preset_flow(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cmd_provider_presets(argparse.Namespace()) == 0
    out = capsys.readouterr().out
    assert "openrouter" in out and "bedrock" in out and "CLOUD" in out

    assert (
        cmd_provider_add(
            argparse.Namespace(
                home=tmp_path,
                provider_id=None,
                preset="zai",
                protocol=None,
                base_url=None,
                model=None,
                api_key_env=None,
                cost_class=None,
                order=0,
                host=None,
                activate=False,
            )
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "registered zai" in out
    assert "CLOUD" in out  # the egress truth prints at register time (I8)
