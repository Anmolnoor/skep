"""v85-F3: skill packs — SKILL.md packs that ship scripts walk the v17 ladder.

An instruction-only pack is inert text and imports directly (v44-F6 /
v83-F12); a pack that ships ``scripts/`` is a *package* and gets the same
governed walk a forged tool gets (v71):

    draft -> sandboxed -> tested -> reviewed -> approved -> active
                                                     \\-> suspended <-> active
    (any state) -> rolled_back

Every edge goes through ``plugin_lifecycle.require_transition`` — the gates
(a passing verifier for ``tested``, a human action for ``approved``) are
enforced by shape. The trial is supervisor-side evidence (I2): every shipped
script is PARSED (``py_compile`` / ``sh -n``), and a pack that DECLARES a
``self_test:`` also has that command run for real, in a sandboxed script run
with deny-all egress (v100-F5, closing R13). The evidence says which level it
rests on, so a syntax-only trial never reads as a behavioural one. A pack has
no runnable surface at all until activation writes the registry template, so
the pre-active states are structurally inert — stronger than sandboxed.

Activation snapshots the pack into ``<skep home>/skills/<pack_id>/`` (the
forge ``install_source`` precedent: the source of truth stops being a
mutable external directory) and writes the template with the operator's
typed grants (provenance ``"pack"``). Suspension removes the template:
registered ⟺ active, no half-states (I8).
"""

from __future__ import annotations

import dataclasses
import json
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .plugin_lifecycle import require_transition
from .skill_md import parse_skill_md, template_from_skill_md
from .store import RunStore
from .templates import WorkflowTemplate

SKILL_PACKS_SETTINGS_KEY = "skill_packs"
PACK_PROVENANCE = "pack"
INSTALLED_PACKS_DIR_NAME = "skills"
# v85-F4: where a run's granted pack files appear inside the workspace. The
# grant argv and the materialized path agree by construction (activation
# rewrites script tokens onto this prefix).
WORKSPACE_PACK_DIR = ".skep-skill"


@dataclass(frozen=True)
class SkillPackRecord:
    """One script-shipping pack and where it stands on the ladder."""

    pack_id: str
    name: str
    description: str
    source_dir: str
    state: str
    scripts: tuple[str, ...] = ()
    grants: tuple[str, ...] = ()  # operator-typed --allow-script commands
    worker_kind: str = "coding"
    origin: str = "import-md"  # or "shelf:<path>"
    trial: dict[str, Any] | None = None
    # v100-F5 (R13): the SKILL.md-declared command the sandboxed trial runs.
    self_test: str = ""


class SkillPackError(ValueError):
    """A refusal that names the acceptable path forward (I9)."""


def installed_packs_root(config: Any) -> Path:
    # config.home is <SKEP_HOME>/supervisor (build_config); skills sits beside it.
    return Path(config.home).parent / INSTALLED_PACKS_DIR_NAME


def load_packs(store: RunStore) -> dict[str, SkillPackRecord]:
    raw = store.get_setting(SKILL_PACKS_SETTINGS_KEY)
    if not raw:
        return {}
    entries = json.loads(raw) if isinstance(raw, str) else raw
    packs: dict[str, SkillPackRecord] = {}
    if isinstance(entries, list):
        for entry in entries:
            if not isinstance(entry, dict) or not entry.get("pack_id"):
                continue
            record = SkillPackRecord(
                pack_id=str(entry["pack_id"]),
                name=str(entry.get("name") or entry["pack_id"]),
                description=str(entry.get("description") or ""),
                source_dir=str(entry.get("source_dir") or ""),
                state=str(entry.get("state") or "draft"),
                scripts=tuple(str(s) for s in entry.get("scripts") or ()),
                grants=tuple(str(g) for g in entry.get("grants") or ()),
                worker_kind=str(entry.get("worker_kind") or "coding"),
                origin=str(entry.get("origin") or "import-md"),
                trial=entry.get("trial") if isinstance(entry.get("trial"), dict) else None,
                self_test=str(entry.get("self_test") or ""),
            )
            packs[record.pack_id] = record
    return packs


def save_pack(store: RunStore, record: SkillPackRecord) -> None:
    packs = load_packs(store)
    packs[record.pack_id] = record
    store.set_setting(
        SKILL_PACKS_SETTINGS_KEY, json.dumps([asdict(p) for p in packs.values()])
    )


def draft_pack(
    store: RunStore,
    directory: Path,
    *,
    grants: tuple[str, ...] = (),
    origin: str = "import-md",
) -> SkillPackRecord:
    """Register a script-shipping pack as a draft. Idempotent by pack name —
    an existing record in ANY state wins (a rolled-back pack is not silently
    re-drafted; the record is the operator's history, I8)."""
    pack = parse_skill_md(directory)
    if not pack.scripts_found:
        raise SkillPackError(
            f"{pack.name!r} ships no scripts — import it directly "
            "(`skep skill import-md --approve`); the ladder is for packages"
        )
    existing = load_packs(store).get(pack.name)
    if existing is not None:
        return existing
    record = SkillPackRecord(
        pack_id=pack.name,
        name=pack.name,
        description=pack.description,
        source_dir=str(directory),
        state="draft",
        scripts=pack.scripts_found,
        grants=grants,
        worker_kind=pack.worker_kind,
        origin=origin,
        self_test=pack.self_test,
    )
    save_pack(store, record)
    return record


def _check_script(path: Path) -> dict[str, Any]:
    """Parse-only: nothing here ever executes the script's code."""
    if not path.is_file():
        return {"ok": False, "check": "exists", "detail": "missing file"}
    suffix = path.suffix.lower()
    if suffix == ".py":
        try:
            compile(path.read_text(encoding="utf-8"), str(path), "exec")
        except (SyntaxError, ValueError, UnicodeDecodeError) as exc:
            return {"ok": False, "check": "compile", "detail": str(exc)}
        return {"ok": True, "check": "compile", "detail": "compiles"}
    if suffix in {".sh", ".bash"}:
        proc = subprocess.run(
            ["sh", "-n", str(path)], capture_output=True, text=True, timeout=30
        )
        if proc.returncode != 0:
            return {"ok": False, "check": "sh -n", "detail": proc.stderr.strip()}
        return {"ok": True, "check": "sh -n", "detail": "parses"}
    try:
        path.read_bytes()
    except OSError as exc:
        return {"ok": False, "check": "readable", "detail": str(exc)}
    return {"ok": True, "check": "readable", "detail": "not syntax-checked"}


# v100-F5 (R13): the pack's own check, run for real. Built on the forge's
# trial template and printing the forge's evidence marker, so forge.trial_verdict
# reads it unchanged — one verdict reader, and the supervisor still parses the
# evidence itself (I2). The pack is extracted at the EXACT path activation
# grants and materialize_packs_for_run uses, so a passing self-test proves the
# layout the real run will see. Both payloads ride base64 so no pack content or
# command text can break out of the template.
_SELF_TEST_HARNESS = '''import base64, io, json, os, shlex, subprocess, tarfile

PACK_DIR = os.path.join(os.getcwd(), "__PACK_PATH__")
os.makedirs(PACK_DIR, exist_ok=True)
with tarfile.open(fileobj=io.BytesIO(base64.b64decode("__PACK_B64__")), mode="r:gz") as tar:
    tar.extractall(PACK_DIR, filter="data")

command = shlex.split(base64.b64decode("__COMMAND_B64__").decode("utf-8"))
evidence = {"ok": False, "self_test": None, "error": None}
try:
    proc = subprocess.run(
        command, cwd=PACK_DIR, capture_output=True, text=True, timeout=300
    )
except (OSError, subprocess.SubprocessError) as exc:
    evidence["error"] = "could not run the self_test: " + str(exc)
else:
    if proc.returncode == 0:
        evidence["ok"] = True
        evidence["self_test"] = (proc.stdout or "ok")[-400:]
    else:
        tail = (proc.stderr or proc.stdout or "")[-400:]
        evidence["error"] = (
            "self_test FAILED (exit " + str(proc.returncode) + "): " + tail
        )
print("FORGE_TRIAL " + json.dumps(evidence), flush=True)
'''

# Inlining a large tree into a run's instructions is not a trial, it is a
# payload — refuse by name and say why (I9).
SELF_TEST_MAX_BYTES = 256 * 1024


def self_test_script(record: SkillPackRecord) -> str:
    """The harness that carries this pack into the sandbox and runs its check."""
    import base64
    import io
    import tarfile

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        tar.add(Path(record.source_dir), arcname=".")
    payload = buffer.getvalue()
    if len(payload) > SELF_TEST_MAX_BYTES:
        raise SkillPackError(
            f"pack {record.pack_id!r} is {len(payload) // 1024}KB packed, over the "
            f"{SELF_TEST_MAX_BYTES // 1024}KB self-test limit — trim the pack "
            "(a trial carries the pack into the sandbox inline), or promote it "
            "on the syntax smoke by dropping its self_test"
        )
    return (
        _SELF_TEST_HARNESS.replace("__PACK_PATH__", f"{WORKSPACE_PACK_DIR}/{record.pack_id}")
        .replace("__PACK_B64__", base64.b64encode(payload).decode("ascii"))
        .replace(
            "__COMMAND_B64__",
            base64.b64encode(record.self_test.encode("utf-8")).decode("ascii"),
        )
    )


def _run_self_test(store: RunStore, config: Any, record: SkillPackRecord) -> tuple[bool, str]:
    """Dispatch the pack's own check on the script lane: sandbox, deny-all
    egress, workspace-only writes. The forge repo is the trial's repo — the
    v83-F14 precedent for a trial with no authoring run — and the caller's
    store is passed through so there is still ONE writer.
    """
    from ..workers.script_worker import script_instructions
    from .dispatch import run_task
    from .forge import ensure_forge_seed, forge_root, trial_verdict
    from .policy_resolver import resolve_run_policy
    from .serve.registry import ensure_repo_baseline

    script = self_test_script(record)  # raises before any dispatch on an oversize pack
    repo = forge_root(config)
    repo.mkdir(parents=True, exist_ok=True)
    ensure_forge_seed(repo)
    ensure_repo_baseline(repo)
    # The trial REQUESTS — script caste, sandbox, deny-all egress — and the
    # resolver decides. Building a Permissions envelope here instead would be a
    # second permission path, the one thing skep does not have (I5); the v36-F3
    # guard in test_policy_compilation is what says so out loud.
    resolved = resolve_run_policy(
        store=store,
        config=config,
        repo=repo,
        caste="script",
        network=[],
        env_allowlist=[],
        wall_clock_seconds=None,
        max_iterations=None,
        max_actions=None,
        max_provider_calls=None,
        execution_mode="sandbox",
    )
    outcome = run_task(
        repo,
        script_instructions("python", script),
        config=config,
        worker_kind="script",
        execution_mode=resolved.execution_mode,
        permissions=resolved.permissions,
        budget=resolved.budget,
        project_context=resolved.project_context,
        store=store,
    )
    task_id = outcome.record.task_id
    output = "\n".join(
        str(event.payload.get("stdout") or "")
        for event in store.events_for(task_id)
        if event.type.value == "command.result"
    )
    passed, detail, _ = trial_verdict(
        {"state": str(outcome.record.state), "output": output, "error": outcome.record.summary}
    )
    return passed, detail


def run_trial(
    record: SkillPackRecord,
    *,
    store: RunStore | None = None,
    config: Any | None = None,
) -> dict[str, Any]:
    """The supervisor's own verifier evidence — never a worker's word (I2).

    The syntax smoke runs for every pack. A pack that DECLARES a ``self_test:``
    additionally runs it for real in a sandboxed, no-network script run — the
    R13 upgrade ADR 0045 named in its own Consequences. ``level`` says which
    evidence the promotion actually rests on, so a syntax-only trial can never
    be read as a behavioural one (I8).
    """
    root = Path(record.source_dir)
    results = [
        {"script": rel, **_check_script(root / rel)} for rel in record.scripts
    ]
    evidence: dict[str, Any] = {
        "ok": bool(results) and all(r["ok"] for r in results),
        "scripts": results,
        "level": "syntax",
        "command": "",
    }
    if not evidence["ok"] or not record.self_test or store is None or config is None:
        return evidence
    passed, detail = _run_self_test(store, config, record)
    evidence["level"] = "self_test"
    evidence["command"] = record.self_test
    evidence["ok"] = passed
    evidence["detail"] = detail
    if not passed:
        evidence["error"] = detail
    return evidence


def install_pack(config: Any, record: SkillPackRecord) -> Path:
    """Snapshot the pack under <skep home>/skills/<pack_id>/ — activation's
    source of truth, immune to later edits of the external directory."""
    source = Path(record.source_dir)
    if not (source / "SKILL.md").is_file():
        raise SkillPackError(
            f"pack source {source} is gone (no SKILL.md) — re-import the pack "
            "from a directory that exists"
        )
    destination = installed_packs_root(config) / record.pack_id
    if destination.exists():
        shutil.rmtree(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination)
    return destination


def _workspace_grants(
    record: SkillPackRecord, grants: tuple[str, ...]
) -> tuple[str, ...]:
    """Rewrite shipped-script tokens onto the workspace materialization path,
    so the granted prefix and the file the worker sees agree by construction."""
    import shlex

    out: list[str] = []
    for command in grants:
        tokens = shlex.split(command)
        rewritten = [
            f"{WORKSPACE_PACK_DIR}/{record.pack_id}/{token}"
            if token in record.scripts
            else token
            for token in tokens
        ]
        out.append(shlex.join(rewritten))
    return tuple(out)


def materialize_packs_for_run(
    store: RunStore,
    config: Any,
    workspace: Path,
    shell_allowlist: Any,
) -> list[str]:
    """v85-F4: copy every ACTIVE pack snapshot a run's grants reference into
    the workspace — workspace-only writes (I12), so the sandbox walls hold on
    every backend. A grant naming a non-active or missing pack copies nothing
    and fails visibly at the shell instead of silently half-working."""
    prefix = WORKSPACE_PACK_DIR + "/"
    wanted: set[str] = set()
    for command in shell_allowlist or ():
        for token in command:
            token_text = str(token)
            if token_text.startswith(prefix):
                parts = token_text.split("/")
                if len(parts) >= 3:
                    wanted.add(parts[1])
    if not wanted:
        return []
    packs = load_packs(store)
    copied: list[str] = []
    for pack_id in sorted(wanted):
        record = packs.get(pack_id)
        if record is None or record.state != "active":
            continue
        snapshot = installed_packs_root(config) / pack_id
        if not snapshot.is_dir():
            continue
        destination = workspace / WORKSPACE_PACK_DIR / pack_id
        if destination.exists():
            shutil.rmtree(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(snapshot, destination)
        copied.append(pack_id)
    return copied


def promote_pack(
    store: RunStore,
    config: Any,
    pack_id: str,
    *,
    extra_grants: tuple[str, ...] = (),
    human_action: bool,
) -> tuple[SkillPackRecord, WorkflowTemplate | None]:
    """Drive the ladder for one pack in one human-authorized step.

    Every edge goes through ``require_transition`` — the v17 gates are
    enforced by shape, exactly the forge precedent (serve/tools.py v71-F1).
    Returns the record plus the activated template (None when already active).
    """
    record = load_packs(store).get(pack_id)
    if record is None:
        known = ", ".join(sorted(load_packs(store))) or "none yet — import-md drafts one"
        raise SkillPackError(f"no skill pack {pack_id!r}; known: {known}")
    if record.state == "active":
        return record, None
    if record.state == "rolled_back":
        raise SkillPackError(
            f"pack {pack_id!r} was rolled back — that is terminal; re-import it "
            "as a fresh draft under a new review"
        )
    grants = tuple(dict.fromkeys((*record.grants, *extra_grants)))
    if record.state == "suspended":
        # Reactivation: already trialed and approved; only the template returns.
        require_transition("suspended", "active")
    else:
        if record.state == "draft":
            require_transition("draft", "sandboxed")
            record = dataclasses.replace(record, state="sandboxed")
            save_pack(store, record)
        evidence = run_trial(record, store=store, config=config)
        record = dataclasses.replace(record, trial=evidence)
        save_pack(store, record)
        if not evidence["ok"]:
            # v100-F5: a self-test failure has no failing SCRIPT to name, so
            # the pack's own error is the message when there is one.
            failed = (
                str(evidence.get("error") or "")
                or "; ".join(
                    f"{r['script']}: {r['detail']}"
                    for r in evidence["scripts"]
                    if not r["ok"]
                )
                or "no scripts found"
            )
            raise SkillPackError(
                f"the trial did not pass ({failed}) — the pack stays 'sandboxed'. "
                "Fix the scripts at the source, then promote again."
            )
        require_transition("sandboxed", "tested", verifier_passed=True)
        require_transition("tested", "reviewed")
        require_transition("reviewed", "approved", human_action=human_action)
        require_transition("approved", "active")
    installed = install_pack(config, record)
    snapshot = parse_skill_md(installed)
    template = dataclasses.replace(
        template_from_skill_md(
            snapshot, allow_scripts=_workspace_grants(record, grants)
        ),
        name=record.pack_id,
        provenance=PACK_PROVENANCE,
    )
    if grants:
        # v85-F4: teach the worker where the granted files live (I9) — the
        # pack is materialized into every run's workspace at dispatch.
        template = dataclasses.replace(
            template,
            instructions=template.instructions
            + f"\n\nThis skill's files are materialized at "
            f"{WORKSPACE_PACK_DIR}/{record.pack_id}/ inside your workspace; "
            "granted script commands use those paths.",
        )
    existing_template = store.get_template(record.pack_id)
    if existing_template is not None and existing_template.provenance != PACK_PROVENANCE:
        raise SkillPackError(
            f"a skill named {record.pack_id!r} already exists "
            f"(provenance {existing_template.provenance!r}) — the operator's copy "
            "wins; rename or delete it before activating this pack"
        )
    if existing_template is not None:
        store.remove_template(record.pack_id)
    store.add_template(template)
    record = dataclasses.replace(record, state="active", grants=grants)
    save_pack(store, record)
    return record, template


def suspend_pack(
    store: RunStore, pack_id: str, *, rollback: bool = False
) -> SkillPackRecord:
    """Pause (or retire) a pack. Removing the registry template IS the
    suspension — registered ⟺ active, no dispatchable half-state (I8)."""
    record = load_packs(store).get(pack_id)
    if record is None:
        raise SkillPackError(f"no skill pack {pack_id!r}; list_skills shows them")
    target = "rolled_back" if rollback else "suspended"
    require_transition(record.state, target)
    template = store.get_template(record.pack_id)
    if template is not None and template.provenance == PACK_PROVENANCE:
        store.remove_template(record.pack_id)
    record = dataclasses.replace(record, state=target)
    save_pack(store, record)
    return record
