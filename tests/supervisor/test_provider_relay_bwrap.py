"""ADR 0062 (V6b) on a REAL bubblewrap sandbox: the Linux proof.

A builtin coding worker runs under ``--unshare-net``. Its only way out is the
netshim → AF_UNIX → filtering proxy bridge, so its model call can only reach
the (fake) provider through the run's relay, which adds the key. The model's
plan writes a probe and nominates it as the verify step, so the probe runs
inside the worker's own sandbox and reports whether it can find the key in
its environment, its parents' environments, the key files, or anything under
the skep home and the run dirs. Keys are built at runtime (CI runs gitleaks).
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sys
from pathlib import Path

import pytest

from skep.supervisor import sandbox
from tests.supervisor.fake_openai import FakeOpenAI

pytestmark = pytest.mark.skipif(
    sandbox.availability().backend != "bubblewrap",
    reason="Linux bubblewrap relay proof (runs where bwrap is the backend)",
)

# Runs inside the worker's sandbox as its verify step. Knows only the key's
# hash, so the plan that carries it to the worker carries no key.
_PROBE = r"""
import hashlib, os, re, sys
from pathlib import Path
want = sys.argv[1]
pat = re.compile(rb"fk-[0-9a-f]{24}")
hits = []
def scan(label, data):
    for m in pat.findall(data):
        if hashlib.sha256(m).hexdigest() == want:
            hits.append(label)
scan("os.environ", "\0".join(f"{k}={v}" for k, v in os.environ.items()).encode())
pid = os.getpid()
while pid > 1:
    try:
        scan(f"/proc/{pid}/environ", Path(f"/proc/{pid}/environ").read_bytes())
        stat = Path(f"/proc/{pid}/stat").read_text()
        pid = int(stat.rsplit(")", 1)[1].split()[1])
    except (OSError, ValueError):
        break
home = Path(os.environ["SKEP_HOME"])
for name in ("llm-secret", "llm-secret-p1"):
    try:
        scan(name, (home / "supervisor" / name).read_bytes())
    except OSError:
        pass
try:  # the token's secret: whoever reads it can mint any run's token
    (home / "supervisor" / "relay-secret").read_bytes()
    hits.append("relay-secret")
except OSError:
    pass
roots = [home, Path.cwd(), Path("/tmp")] + [Path(a).parent for a in sys.argv[2:]]
for root in roots:
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            try:
                scan(os.path.join(dirpath, f), Path(dirpath, f).read_bytes())
            except OSError:
                pass
if hits:
    sys.exit("KEY SEEN: " + ", ".join(sorted(set(hits))))
print("clean")
"""


def _plan(key_hash: str) -> str:
    return json.dumps(
        {
            "summary": "probe for the key",
            "files": [{"path": "probe.py", "content": _PROBE}],
            "verify": {
                "argv": [sys.executable, "probe.py", key_hash],
                "expected_stdout": "clean\n",
            },
        }
    )


@pytest.mark.parametrize(
    ("relay_on", "network"),
    [(True, ["pypi.org"]), (True, ["*"]), (False, ["pypi.org"])],
    ids=["on-netshim", "on-allow-all", "off"],
)
def test_a_relayed_worker_on_real_bwrap_reaches_its_model_and_never_sees_the_key(
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    relay_on: bool,
    network: list[str],
) -> None:
    """A domain list runs under --unshare-net (netshim → unix door → relay);
    allow-all gets a relay-only proxy on host loopback. Off is the control:
    the same probe finds the key, so "clean" is not a probe that cannot see."""
    from skep.profile import run_personal_setup
    from skep.supervisor.cli_cmds import build_config
    from skep.supervisor.dispatch import run_task
    from skep.supervisor.serve.llm import store_api_key
    from skep.supervisor.store import RunStore
    from skep.worker_contract import Permissions

    key = "fk-" + secrets.token_hex(12)
    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    # The key on the worker's env allowlist too: the relay must drop it by value.
    monkeypatch.setenv("SKEP_TEST_RELAY_KEY", key)
    config = build_config(tmp_path / "home", None)
    store_api_key(config.home, key)  # the llm-secret file the sandbox must mask
    server = FakeOpenAI(api_key=key).start()
    try:
        run_personal_setup(
            config.home.parent, provider="openai-compat", model="gpt-oss", endpoint=server.base_url
        )
        store = RunStore(config.db_path)
        store.set_setting("provider_key_relay", relay_on)
        store.close()
        for _ in range(4):  # a failed verify asks the model for a repair
            server.script_reply(_plan(hashlib.sha256(key.encode()).hexdigest()))
        outcome = run_task(
            repo,
            "Probe for the key.",
            config=config,
            permissions=Permissions(
                read=["workspace"],
                write=["workspace"],
                network=network,
                env_allowlist=["SKEP_TEST_RELAY_KEY"],
                shell_allowlist=[[sys.executable, "probe.py"]],
            ),
            execution_mode="sandbox",
        )
    finally:
        server.stop()
    record = outcome.record
    store = RunStore(config.db_path)
    try:
        dispatched = [
            d for state, d, _ in store.transitions_for(record.task_id) if state == "dispatched"
        ]
    finally:
        store.close()
    # The model call went netshim -> proxy -> relay -> provider, with the key.
    assert server.requests, "the worker's model call never reached the provider"
    assert server.requests[-1]["headers"]["Authorization"] == f"Bearer {key}"
    worker_log = (config.audit_dir / record.task_id / "worker.log").read_text()
    assert (record.summary or "").startswith("probe for the key"), (record, worker_log)
    if not relay_on:
        assert "provider_key_relay" not in (dispatched[0] or "")
        assert record.verification_outcome == "failed", record
        # The probe's own report (kept in the run's decision log) names what it saw.
        log = b"".join(p.read_bytes() for p in config.home.glob("supervisor.sqlite3*"))
        seen = log[log.find(b"KEY SEEN") :].split(b"\\n", 1)[0]  # JSON-escaped
        assert all(s in seen for s in (b"os.environ", b"/environ", b"llm-secret")), seen
        return
    assert '"provider_key_relay": "on"' in (dispatched[0] or "")
    # The probe ran inside the sandbox and saw no key anywhere it could read.
    assert record.verification_outcome == "passed", (record.verification_details, worker_log)
    assert "differed" not in (record.verification_details or ""), record.verification_details
    # And nothing the run left on the host carries the key (Q3: logs, record).
    for path in config.home.parent.rglob("*"):
        if path.is_file() and path.name != "llm-secret":
            assert key.encode() not in path.read_bytes(), path
