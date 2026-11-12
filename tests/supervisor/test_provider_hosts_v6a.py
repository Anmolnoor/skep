"""ADR 0059 D3 (V6a): skep's own LLM endpoint is a supervisor resource.

``Permissions.provider_hosts`` holds it as ``host:port``, beside — never in —
``network``, so the delegation narrowing never sees it (B12), an external
engine gets only its own host (B140), and the proxy admits it on that one
port (f1-30)."""

from __future__ import annotations

from pathlib import Path

import pytest

from skep.supervisor.netproxy import admit_target, domain_allowed
from skep.worker_contract import CodingWorkerTask, Permissions
from skep.workers.llm_plan import LlmPlanError, _ensure_network_allowed


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


# --- matching ---------------------------------------------------------------


def test_a_host_port_entry_matches_only_that_port() -> None:
    allow = ("ollama.com:443",)
    assert domain_allowed("ollama.com", allow, port=443)
    assert not domain_allowed("ollama.com", allow, port=80)
    # A port-less question (a capability-layer host check) never matches a
    # pinned entry: the provider is not a host grant.
    assert not domain_allowed("ollama.com", allow)
    # Plain entries keep their any-port meaning.
    assert domain_allowed("pypi.org", ("pypi.org",), port=8443)


def _resolve_public(host: str, port: int, **_: object) -> list[tuple[object, ...]]:
    return [(None, None, None, "", ("93.184.216.34", port))]


def test_proxy_admits_the_provider_port_and_refuses_another() -> None:
    allow = ("ollama.com:443",)
    ok, _, _ = admit_target("ollama.com", 443, allow, denied_ports=(), resolve=_resolve_public)
    assert ok == 200
    status, reason, _ = admit_target(
        "ollama.com", 8443, allow, denied_ports=(), resolve=_resolve_public
    )
    assert status == 403 and "not in the task network allowlist" in reason


def test_a_loopback_provider_never_opens_serve() -> None:
    """f1-30 / B05: a provider on localhost:11434 is reachable; serve's port
    stays refused whatever the allowlist says."""
    allow = ("localhost:11434",)
    assert admit_target("localhost", 11434, allow, denied_ports=(8765,))[0] == 200
    status, reason, _ = admit_target("localhost", 8765, allow, denied_ports=(8765,))
    assert status == 403 and "control plane" in reason
    # Even a provider entry naming serve's port does not open it.
    status, _, _ = admit_target("localhost", 8765, ("localhost:8765",), denied_ports=(8765,))
    assert status == 403


# --- the worker's own provider check ----------------------------------------


@pytest.mark.parametrize(
    ("endpoint", "network", "provider_hosts"),
    [
        ("http://localhost:11434", [], ["localhost:11434"]),
        ("https://ollama.com", [], ["ollama.com:443"]),
        ("https://ollama.com/api", ["pypi.org"], ["ollama.com:443"]),
        # a task persisted before 0.3.6 carried the host in network
        ("https://ollama.com", ["ollama.com"], []),
    ],
)
def test_worker_provider_check_reads_provider_hosts_or_network(
    endpoint: str, network: list[str], provider_hosts: list[str]
) -> None:
    _ensure_network_allowed(endpoint, network, provider_hosts)


def test_worker_provider_check_refuses_a_provider_on_another_port() -> None:
    with pytest.raises(LlmPlanError, match=r"provider host 'localhost:9999'.*localhost:11434"):
        _ensure_network_allowed("http://localhost:9999", [], ["localhost:11434"])


def test_worker_provider_check_with_nothing_says_where_the_host_comes_from() -> None:
    with pytest.raises(
        LlmPlanError, match="a provider host or a task network allowlist; this task has neither"
    ):
        _ensure_network_allowed("https://ollama.com", [], [])


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


# --- the sandbox/proxy feed and the worker's capability layer ---------------


def _task(**perms: object) -> CodingWorkerTask:
    return CodingWorkerTask.model_validate(
        {
            "contract_version": "0.3.6",
            "task_id": "t",
            "trace_id": "t",
            "worker_kind": "coding",
            "workspace": "/w",
            "instructions": "x",
            "permissions": {"read": [], "write": [], "env_allowlist": [], **perms},
            "budget": {
                "wall_clock_seconds": 1,
                "max_iterations": 1,
                "max_actions": 1,
                "max_provider_calls": 1,
            },
        }
    )


def test_a_provider_only_run_feeds_the_proxy_its_provider_and_nothing_else() -> None:
    """network: [] + a provider = one filtered pipe to that host:port, not
    deny-all (the worker could not plan) and not allow-all."""
    from skep.supervisor.spawner import effective_network_domains

    assert effective_network_domains(_task(network=[], provider_hosts=["ollama.com:443"])) == (
        "ollama.com:443",
    )
    assert effective_network_domains(_task(network=["*"], provider_hosts=["x:1"])) == ("*",)
    assert effective_network_domains(_task(network=[])) == ()


def test_the_capability_layer_does_not_treat_the_provider_as_a_fetch_grant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The provider is egress, so the run is not "allowlist missing" (which
    would card): a fetch elsewhere stays the flat deny it was when the host
    rode network — and the provider itself is no fetch target. Shell children
    keep the proxy env, so in workspace mode they stay on the filtered pipe."""
    from skep.workers.capabilities import CapabilityRegistry

    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    registry = CapabilityRegistry(
        tmp_path, emit=lambda _t, _p: None, provider_hosts=("ollama.com:443",)
    )
    assert registry._network_fetch_decision("example.com").verdict == "deny"
    assert registry._network_fetch_decision("ollama.com").verdict == "deny"
    assert registry._child_env.get("HTTPS_PROXY") == "http://127.0.0.1:1"
    bare = CapabilityRegistry(tmp_path, emit=lambda _t, _p: None)
    assert bare._network_fetch_decision("example.com").verdict == "require_approval"
    assert "HTTPS_PROXY" not in bare._child_env


@pytest.mark.parametrize("module", ["document", "reviewer"])
def test_document_and_reviewer_reach_the_provider_through_provider_hosts(
    module: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 0.3.6 task: network [] and the provider only in provider_hosts."""
    import importlib

    from skep.profile import ProviderProfile
    from skep.workers.llm_plan import WorkerProvider

    worker = importlib.import_module(f"skep.workers.{module}")
    provider = WorkerProvider(
        ProviderProfile(name="ollama", model="m", endpoint="http://localhost:11434")
    )
    monkeypatch.setattr(worker, "worker_provider_from_env", lambda: provider)
    monkeypatch.setattr(worker, "_provider_chunks", lambda *_a, **_k: ["ok"])
    task = _task(network=[], provider_hosts=["localhost:11434"])
    assert worker._default_generate(task, []) == "ok"
    with pytest.raises(LlmPlanError, match="localhost:11434"):
        worker._default_generate(_task(network=[], provider_hosts=["localhost:1"]), [])


def test_an_endpoint_url_with_a_bad_port_is_no_provider_host() -> None:
    from skep.supervisor.provider_hosts import _aux_endpoint, _url_host

    assert _url_host("http://localhost:99999") is None
    assert _url_host("ftp://x") is None
    assert _url_host("https://[::1]:8443/v1") == "[::1]:8443"
    assert _aux_endpoint("*.example.com") is None
    assert _aux_endpoint("auth.example:8443") == "auth.example:8443"
