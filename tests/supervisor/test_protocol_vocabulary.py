"""v108-F1: the protocol vocabulary has ONE source of truth.

The protocol names were hardcoded in ~9 places with two spellings;
``gemini`` sat in the registry vocabulary with no client, probe, or worker
mapping behind it (a stored profile was a dead end), and ``anthropic`` was
missing from the probe bridge so an anthropic registry profile could never
probe healthy. These tests pin every derived surface to the ``LLMProtocol``
Literal: the next protocol grows the Literal plus ``REGISTRY_PROTOCOLS``
and then follows the failures here to the UI selects and the chat tool.
"""

from __future__ import annotations

from typing import get_args

import pytest

from skep.supervisor.providers import (
    _LEGACY_PROTOCOL_MAP,
    PROVIDER_PROTOCOLS,
    ProviderError,
    ProviderProfile,
    validate_provider_profile,
)
from skep.supervisor.serve.app import STATIC_DIR
from skep.supervisor.serve.llm import REGISTRY_PROTOCOLS, LLMProtocol, _protocol
from skep.supervisor.serve.tools import MUTATING_TOOL_SPECS


def _profile(**kw: object) -> ProviderProfile:
    base: dict[str, object] = {
        "provider_id": "p",
        "protocol": "openai_compat",
        "base_url": "https://api.example.com/v1",
        "model": "m",
        "cost_class": "paid",
    }
    base.update(kw)
    return ProviderProfile(**base)  # type: ignore[arg-type]


def test_registry_and_serve_spellings_agree() -> None:
    assert set(REGISTRY_PROTOCOLS) == set(PROVIDER_PROTOCOLS)
    assert set(REGISTRY_PROTOCOLS.values()) == set(get_args(LLMProtocol))
    # The legacy serve->registry map is exactly the inverse of the bridge.
    assert {serve: reg for reg, serve in REGISTRY_PROTOCOLS.items()} == _LEGACY_PROTOCOL_MAP


def test_protocol_guard_follows_the_literal() -> None:
    for value in get_args(LLMProtocol):
        assert _protocol(value) == value
    # Unknown values fall back (legacy settings rows must keep the daemon
    # bootable) — they are rejected at the registry write path instead.
    assert _protocol("gemini") == "ollama"


def test_gemini_profile_is_rejected_not_stored() -> None:
    with pytest.raises(ProviderError) as err:
        validate_provider_profile(_profile(protocol="gemini"))
    assert "protocol" in str(err.value)


def test_registry_api_key_env_rejects_pasted_key_values() -> None:
    ok = validate_provider_profile(_profile(api_key_env="OPENROUTER_API_KEY"))
    assert ok.api_key_env == "OPENROUTER_API_KEY"
    with pytest.raises(ProviderError) as err:
        validate_provider_profile(_profile(api_key_env="sk-or-v1-abc123.def456"))
    assert "pasted" in str(err.value)


def test_ui_selects_offer_every_protocol() -> None:
    source = (STATIC_DIR / "app.js").read_text()
    for protocol in get_args(LLMProtocol):
        needle = f'el("option", {{ value: "{protocol}" }}'
        # Two protocol <select> blocks: the setup wizard and Settings.
        assert source.count(needle) >= 2, f"{protocol!r} missing from a protocol <select>"


def test_chat_tool_enum_and_prose_follow_the_literal() -> None:
    spec = next(t for t in MUTATING_TOOL_SPECS if t["function"]["name"] == "set_assistant_model")
    enum = spec["function"]["parameters"]["properties"]["protocol"]["enum"]
    assert set(enum) == set(get_args(LLMProtocol))
    # I9: the description is load-bearing — the Queen reads nothing else.
    description = spec["function"]["description"]
    for protocol in get_args(LLMProtocol):
        assert protocol in description
