"""ADR 0059 D3 (V6a): skep's own LLM endpoint is a supervisor resource.

``Permissions.provider_hosts`` holds it as ``host:port``, beside — never in —
``network``, so the delegation narrowing never sees it (B12), an external
engine gets only its own host (B140), and the proxy admits it on that one
port (f1-30)."""

from __future__ import annotations

import pytest

from skep.worker_contract import CodingWorkerTask, Permissions


def _perms(**kw: object) -> Permissions:
    return Permissions.model_validate({"read": [], "write": [], "env_allowlist": [], **kw})


# --- the contract ---------------------------------------------------------


def test_provider_hosts_defaults_empty_for_a_task_persisted_before_it() -> None:
    """A task.json written before contract 0.3.6 has no field — it loads."""
    assert _perms(network=["ollama.com"]).provider_hosts == []


@pytest.mark.parametrize(
    "entry", ["ollama.com:443", "localhost:11434", "127.0.0.1:8080", "[::1]:11434"]
)
def test_provider_hosts_accepts_host_port(entry: str) -> None:
    assert _perms(provider_hosts=[entry]).provider_hosts == [entry]


@pytest.mark.parametrize(
    "entry",
    [
        "ollama.com",  # no port
        "*:443",  # a wildcard is a grant, not an endpoint
        "*.ollama.com:443",
        "https://ollama.com:443",
        "ollama.com:0",
        "ollama.com:70000",
        "ollama.com:https",
        "a b:443",
        ":443",
    ],
)
def test_provider_hosts_rejects_anything_but_host_port(entry: str) -> None:
    with pytest.raises(ValueError, match="provider_hosts"):
        _perms(provider_hosts=[entry])


def test_egress_is_network_plus_provider_hosts_unless_allow_all() -> None:
    assert _perms(network=[], provider_hosts=["ollama.com:443"]).egress() == ["ollama.com:443"]
    assert _perms(network=["pypi.org"], provider_hosts=["ollama.com:443"]).egress() == [
        "pypi.org",
        "ollama.com:443",
    ]
    assert _perms(network=["*"], provider_hosts=["ollama.com:443"]).egress() == ["*"]
    assert _perms(network=[]).egress() == []


def test_old_task_envelope_without_provider_hosts_validates() -> None:
    task = CodingWorkerTask.model_validate(
        {
            "contract_version": "0.3.5",
            "task_id": "t",
            "trace_id": "t",
            "worker_kind": "coding",
            "workspace": "/w",
            "instructions": "x",
            "permissions": {
                "read": ["workspace"],
                "write": ["workspace"],
                "network": ["ollama.com"],
                "env_allowlist": [],
            },
            "budget": {
                "wall_clock_seconds": 1,
                "max_iterations": 1,
                "max_actions": 1,
                "max_provider_calls": 1,
            },
        }
    )
    assert task.permissions.provider_hosts == []
    assert task.permissions.egress() == ["ollama.com"]
