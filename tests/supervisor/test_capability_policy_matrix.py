from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from skep.workers.capabilities import CapabilityRegistry, PluginToolSpec

MATRIX_FIXTURE = Path(__file__).parents[1] / "fixtures" / "capability_policy_matrix.json"


def _plugin_tools(tmp_path: Path) -> tuple[PluginToolSpec, ...]:
    script = tmp_path / "plugin.py"
    script.write_text("print('{}')\n", encoding="utf-8")
    return (
        PluginToolSpec(
            plugin_id="reader",
            tool_id="reader.peek",
            description="Read through plugin.",
            risk="read",
            command=("python", str(script)),
        ),
        PluginToolSpec(
            plugin_id="writer",
            tool_id="writer.write",
            description="Write through plugin.",
            risk="write",
            command=("python", str(script)),
        ),
        PluginToolSpec(
            plugin_id="git",
            tool_id="git_plugin.commit",
            description="Git through plugin.",
            risk="git",
            command=("python", str(script)),
        ),
        PluginToolSpec(
            plugin_id="net",
            tool_id="net.fetch",
            description="Network through plugin.",
            risk="network",
            command=("python", str(script)),
        ),
        PluginToolSpec(
            plugin_id="external",
            tool_id="external.post",
            description="External side effect.",
            risk="external_side_effect",
            command=("python", str(script)),
        ),
    )


def _registry(tmp_path: Path, row: dict[str, Any]) -> CapabilityRegistry:
    return CapabilityRegistry(
        tmp_path,
        emit=lambda _type, _payload: None,
        shell_allowlist=row.get("shell_allowlist", ()),
        allow_git_mutation=bool(row.get("allow_git_mutation", False)),
        network_allowlist=row.get("network_allowlist", ()),
        allowed_plugin_risks=row.get("allowed_plugin_risks", ()),
        approved_capability_ids=row.get("approved_capability_ids", ()),
        approved_network_hosts=row.get("approved_network_hosts", ()),
        approved_plugin_risks=row.get("approved_plugin_risks", {}),
        plugin_tools=_plugin_tools(tmp_path),
    )


def _arguments(row: dict[str, Any]) -> dict[str, object]:
    capability = str(row["capability"])
    if capability == "shell.run":
        return {
            "argv": row.get("argv", ["echo", "ok"]),
            "purpose": row.get("purpose", "modify"),
        }
    if capability.startswith("git."):
        if capability == "git.commit":
            return {"message": "commit from matrix"}
        return {"paths": ["existing.py"]}
    if capability in {"network.fetch", "network.read"}:
        return {"url": row.get("url", "https://example.com/")}
    return {}


def test_capability_policy_matrix_decisions(tmp_path: Path) -> None:
    rows = json.loads(MATRIX_FIXTURE.read_text(encoding="utf-8"))
    assert isinstance(rows, list)

    for row in rows:
        registry = _registry(tmp_path, row)
        decision = registry.decision_for(str(row["capability"]), _arguments(row))
        assert decision.to_payload() == {
            "verdict": row["verdict"],
            "reason": row["reason"],
            "detail": row.get("detail"),
        }, row["name"]
