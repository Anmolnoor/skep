"""first-run O3 (ADR 0056 D7/D8): `skep onboard` as five resumable steps.

model → workdir → sandbox → template → role. Each step is one function that
writes its record under the personal home (``onboard.json``; the role goes to
``first_run.json`` for O4/R1) and calls the verbs that already exist —
``add_provider``/``use_provider``/``run_personal_setup`` for the model,
``setup_project_record`` for the work directory, ``update_policy`` for the
sandbox backend, ``approve_policy_baseline`` for the template. Re-running
resumes at the first incomplete step. The CLI (``skep onboard``) and the REST
routes behind the web ``/setup`` page call these same functions (ADR 0050).
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from . import sandbox
from .policy_baseline import NoBaseline, Ok, Tampered, sha256_of, verify
from .policy_templates import PolicyTemplate, load_policy_template
from .serve.settings import SANDBOX_BACKEND, SANDBOX_BACKENDS, ConfigHolder
from .store import RunStore

STEPS: tuple[str, ...] = ("model", "workdir", "sandbox", "template", "role")
# ADR 0061 D47 (2026-11-21) reverses plan decision #4: `assistant` denies code
# edits, so pressing Enter through onboard left a coding supervisor that could
# not code. `builder` still asks on every command it does not name.
DEFAULT_TEMPLATE = "builder"
OLLAMA_LOCAL = "http://localhost:11434"
_LOOPBACK = {"localhost", "127.0.0.1", "::1"}
STATE_FILE = "onboard.json"
FIRST_RUN_FILE = "first_run.json"


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _personal_home(holder: ConfigHolder) -> Path:
    return holder.current.home.parent


def _read(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _record(home: Path, step: str, record: dict[str, Any]) -> dict[str, Any]:
    state = _read(home / STATE_FILE)
    stamped = {**record, "at": _now()}
    state[step] = stamped
    _write(home / STATE_FILE, state)
    return stamped


def onboard_status(supervisor_home: Path) -> dict[str, Any]:
    """Every step's record (or None) and the first incomplete one. The
    template step is complete only while the baseline verifies AND is what
    ④ would lock now for the recorded template under the recorded sandbox —
    a tampered lock, or a sandbox switch after the lock (O3-F1, D8), sends
    onboard back to step ④. ``relock`` names the template in the second case.

    Phase 05 (5.1): the ONE setup truth — ``/api/onboard``, the CLI and
    ``/api/setup/status`` all read it. A home set up before onboard existed
    is reconciled first (``reconcile_onboard``), and a work directory that
    is gone from disk is not done."""
    home = supervisor_home.parent
    state = reconcile_onboard(supervisor_home)
    first_run = _read(home / FIRST_RUN_FILE)
    records: dict[str, Any] = {step: state.get(step) for step in STEPS[:4]}
    if records["workdir"] is not None and not Path(str(records["workdir"]["path"])).is_dir():
        records["workdir"] = None
    relock = None
    if records["template"] is not None:
        baseline = verify(supervisor_home)
        if not isinstance(baseline, Ok):
            records["template"] = None
        else:
            template = str(records["template"]["template"])
            backend = (records["sandbox"] or {}).get("backend")
            candidate = baseline_candidate(template, backend)
            schema = baseline.lock.get("schema_version")
            if sha256_of(candidate, schema_version=schema) != baseline.lock.get("sha256"):
                records["template"], relock = None, template
    records["role"] = first_run if first_run.get("role") else None
    pending = [step for step in STEPS if records[step] is None]
    return {
        "steps": records,
        "next": pending[0] if pending else None,
        "done": not pending,
        "relock": relock,
    }


def reconcile_onboard(supervisor_home: Path) -> dict[str, Any]:
    """Phase 05 (5.1): fill the onboard records a pre-onboard home already
    proves, so ``/api/setup/status`` ("complete" since 07-08) and
    ``/api/onboard`` ("0 of 5") stop disagreeing. Idempotent: a step that
    has a record is never touched, so a second call writes nothing.

    - ② workdir: a project bound to a repo folder that exists;
    - ③ sandbox: a ``sandbox_backend`` the store holds explicitly;
    - ④ template: ONLY a baseline that already verifies — this never
      approves anything; a home with no approved baseline stays at ④.

    Never ① (a model record means a test call passed — L2's keep path
    records it) and never ⑤ (``first_run.json`` is the operator's answer).
    Returns onboard.json's state after the fill."""
    home = supervisor_home.parent
    state = _read(home / STATE_FILE)
    found: dict[str, dict[str, Any]] = {}
    db = supervisor_home / "supervisor.sqlite3"
    if (state.get("workdir") is None or state.get("sandbox") is None) and db.is_file():
        from .projects import list_projects

        store = RunStore(db, readonly=True)
        try:
            backend = store.get_setting(SANDBOX_BACKEND)
            bound = sorted(
                (project.project_id != "workspace", project.project_id, binding.value, project)
                for project in list_projects(store)
                for binding in project.bindings
                if binding.kind == "repo_path" and Path(binding.value).is_dir()
            )
        finally:
            store.close()
        if state.get("workdir") is None and bound:
            _, project_id, path, project = bound[0]
            found["workdir"] = {
                "path": path,
                "project_id": project_id,
                "created": False,
                "git": (Path(path) / ".git").exists(),
                "verify_command": str(project.policy.get("verify_command") or ""),
            }
        if state.get("sandbox") is None and backend in SANDBOX_BACKENDS:
            found["sandbox"] = {"backend": backend, "rings": rings_line(str(backend))}
    if state.get("template") is None:
        from .policy_templates import TEMPLATE_NAMES

        baseline = verify(supervisor_home)
        if isinstance(baseline, Ok) and baseline.layer.template in TEMPLATE_NAMES:
            found["template"] = {
                "template": baseline.layer.template,
                "sha256": baseline.lock.get("sha256"),
            }
    if not found:
        return state
    for step, record in found.items():
        state[step] = {**record, "reconciled": True, "at": _now()}
    _write(home / STATE_FILE, state)
    return state


# --- ① model -----------------------------------------------------------------


def detect_ollama(endpoint: str = OLLAMA_LOCAL, timeout: float = 1.0) -> list[str] | None:
    """The local Ollama's model list, or None when nothing answers."""
    from .serve.llm import OllamaError, list_models

    try:
        return list_models(endpoint, None, protocol="ollama", timeout=timeout)
    except OllamaError:
        return None


def _preset_for(provider: str, endpoint: str | None, model: str, api_key_env: str | None) -> Any:
    from .provider_presets import PROVIDER_PRESETS, ProviderPreset

    if provider in PROVIDER_PRESETS:
        return PROVIDER_PRESETS[provider]
    if provider == "ollama":
        url = endpoint or OLLAMA_LOCAL
        local = (urlparse(url).hostname or "") in _LOOPBACK
        return ProviderPreset(
            "ollama",
            "Ollama" + (" (local)" if local else ""),
            "ollama",
            url,
            model,
            api_key_env,
            cost_class="local" if local else "paid",
        )
    if provider in ("openai-compatible", "openai_compat"):
        if not endpoint:
            raise ValueError("an openai-compatible provider needs --endpoint")
        return ProviderPreset(
            "openai-compatible", "OpenAI-compatible", "openai_compat", endpoint, model, api_key_env
        )
    raise ValueError(
        f"unknown provider {provider!r}; known: mock, ollama, openai-compatible, "
        "or a preset id from `skep provider presets`"
    )


def _key_fix(
    candidate: Any, *, key_env: str | None, registered: bool, home: Path | None
) -> str | None:
    """The 401/403 fix that is true for this candidate (I9). None keeps the
    default text: a registered profile that takes a stored key, where
    `skep provider set-key` works. ``home`` is the skep home when it is not
    the default one — the commands then carry it."""
    from urllib.parse import urlsplit

    from .codex_login import NO_KEY_REFUSAL, uses_codex_login

    again = "then run the same skep onboard command again"
    pid = candidate.provider_id
    if uses_codex_login(candidate.base_url):
        return f"{pid!r} {NO_KEY_REFUSAL}, {again}"
    if candidate.protocol == "bedrock":
        return (
            "Bedrock takes no key: it signs with the AWS credentials in this environment "
            "(AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, and AWS_SESSION_TOKEN if you use "
            f"one) — set working ones, {again}"
        )
    if registered:
        return None
    if key_env:
        return f"set {key_env} to a working key (it is unset or holds a refused one), {again}"
    skep = shlex.join(["skep", *(["--home", str(home)] if home else [])])
    # a password in the endpoint is never printed back; the rest is quoted so
    # the line is one a shell and argparse take (an IPv6 host, a spaced model)
    base_url = (
        "--base-url=<the endpoint URL you gave>"
        if "@" in urlsplit(candidate.base_url).netloc
        else shlex.quote(f"--base-url={candidate.base_url}")
    )
    add = (
        f"{skep} provider add {shlex.quote(pid)} --protocol {candidate.protocol} "
        f"{base_url} {shlex.quote(f'--model={candidate.model}')}"
    )
    return (
        f"{pid!r} is not registered yet, so there is no stored key to replace: run {add}, "
        f"then {skep} provider set-key {shlex.quote(pid)}, {again} "
        "(or pass --api-key-env NAME for a variable that holds the key)"
    )


def onboard_model(
    holder: ConfigHolder,
    store: RunStore,
    *,
    provider: str,
    model: str,
    endpoint: str | None = None,
    api_key_env: str | None = None,
) -> dict[str, Any]:
    """① Record the provider (the env-var NAME of its key, never the key),
    make a test call, and say where prompts go. ``mock`` is the stub for
    tests and dry runs: profile only, no call."""
    from ..profile import run_personal_setup

    home = _personal_home(holder)
    provider = provider.strip()
    if provider == "mock":
        run_personal_setup(home, provider="mock", model=model or "mock")
        return _record(
            home,
            "model",
            {
                "provider": "mock",
                "model": model or "mock",
                "endpoint": None,
                "api_key_env": None,
                "test": "mock provider: no call made",
                "egress": "mock — no call leaves this machine",
            },
        )
    from .provider_presets import preset_egress_note
    from .providers import (
        ProviderProfile,
        check_provider_health,
        live_probes,
        validate_provider_profile,
    )
    from .serve.actions import add_provider, use_provider
    from .serve.llm import provider_rollback

    preset = _preset_for(provider, endpoint, model, api_key_env)
    base_url = (endpoint or preset.base_url or "").strip()
    model = (model or preset.default_model).strip()
    if not model:
        raise ValueError("name a --model")
    key_env = api_key_env or preset.api_key_env
    # R2 (F3): the preset's id — unless it is registered for another endpoint,
    # whose key stays there: then this endpoint gets the first free <id>-N
    pid = next(
        c
        for c in (preset.preset_id, *(f"{preset.preset_id}-{n}" for n in range(2, 1000)))
        if (held := store.get_provider_profile(c)) is None or held.base_url == base_url.rstrip("/")
    )

    # Phase 05 (5.1): stage the candidate — exactly the profile the switch
    # below writes, with the credential it would resolve — and probe it (the
    # model list AND a 1-token completion) BEFORE anything is written. A
    # refused key (ollama.com lists models to anyone, then 401s the chat)
    # leaves the live provider byte-for-byte as it was (I8). R1: validated
    # first (a pasted key in --api-key-env, a bad URL) — never sent anywhere.
    # The probe resolves the credential exactly as the builtin worker will
    # (named env var → the profile's key file → llm-secret for its own
    # endpoint; workers read this registry profile first, P10).
    candidate = validate_provider_profile(
        ProviderProfile(
            provider_id=pid,
            protocol=preset.protocol,
            base_url=base_url,
            model=model,
            api_key_env=key_env,
            cost_class=preset.cost_class,
        )
    )
    # I9: an unregistered candidate has no stored key for set-key to replace
    default_home = Path(os.environ.get("SKEP_HOME", Path.home() / ".skep"))
    health = check_provider_health(
        candidate,
        now=_now(),
        key_fix=_key_fix(
            candidate,
            key_env=key_env,
            registered=store.get_provider_profile(pid) is not None,
            home=None if home.resolve() == default_home.resolve() else home,
        ),
        **live_probes(holder.current.home, store, timeout=15.0),
    )
    if not health.ok:
        previous = store.active_provider_profile()
        kept = (
            f"the previous provider {previous.provider_id!r} is still active"
            if previous is not None
            else "no provider was active before and none is now"
        )
        raise ValueError(f"test call failed: {health.error} — nothing changed: {kept}")

    # L2: what the switch touches is put back if anything fails mid-write
    # (an interrupt included) — a failed switch changes nothing (I8). Phase
    # 05: the test call already ran, on the candidate.
    previous = store.active_provider_profile()
    try:
        with provider_rollback(store, holder.current.home):
            if preset.preset_id in ("ollama", "openai-compatible"):
                add_provider(
                    store,
                    provider_id=pid,
                    protocol=preset.protocol,
                    base_url=base_url,
                    model=model,
                    api_key_env=key_env,
                    cost_class=preset.cost_class,
                    source="onboard",
                )
            else:
                add_provider(
                    store,
                    preset=preset.preset_id,
                    provider_id=pid,
                    base_url=base_url,
                    model=model,
                    api_key_env=key_env,
                )
            used = use_provider(store, holder.current.home, provider_id=pid, probe=False)
            # use_provider writes profile.json through with no key env (v19-F9); put
            # the operator's NAME back so doctor and workers read the same truth.
            run_personal_setup(
                home, provider=used["protocol"], model=model, endpoint=base_url, api_key_env=key_env
            )
    except Exception as exc:
        kept = (
            f"the previous provider {previous.provider_id!r} is still active"
            if previous is not None
            else "no provider was active before and none is now"
        )
        raise ValueError(f"{exc} — nothing changed: {kept}") from exc
    return _record(
        home,
        "model",
        {
            "provider": provider,
            "model": model,
            "endpoint": base_url,
            "api_key_env": key_env,
            "test": f"provider ready ({model} via {base_url})"
            + ("" if health.model_found else f" — {model!r} is not in its model list"),
            "egress": preset_egress_note(preset, base_url),
        },
    )


def onboard_keep_model(holder: ConfigHolder) -> dict[str, Any]:
    """① with no provider named (L2): test the provider already configured
    and, when it works, record it as the model step — onboard never replaces
    a working provider with one it merely detected. Raises ``ValueError``
    when there is nothing working to keep."""
    from ..status import worker_provider_check
    from ..workers.llm_plan import worker_provider_from_home
    from .providers import provider_host

    home = _personal_home(holder)
    check = worker_provider_check(home, 5.0)
    provider = worker_provider_from_home(home) if check.get("status") == "ready" else None
    if provider is None:
        raise ValueError(f"no working provider to keep ({check.get('label')})")
    profile = provider.profile
    host = provider_host(profile.endpoint or "") or "?"
    egress = (
        f"LOCAL ({host}) — nothing leaves this machine"
        if host in _LOOPBACK
        else f"CLOUD — every assistant/worker prompt leaves this machine for {host}"
    )
    return _record(
        home,
        "model",
        {
            "provider": profile.name,
            "model": profile.model,
            "endpoint": profile.endpoint,
            "api_key_env": profile.api_key_env,
            "test": str(check.get("label")),
            "egress": egress,
            "kept": True,
        },
    )


# --- ② work directory ------------------------------------------------------------


def onboard_workdir(holder: ConfigHolder, store: RunStore, *, path: str) -> dict[str, Any]:
    """② Create the directory if missing and register it through project
    setup, which infers the pinned ``verify_command`` (v91-F1). A directory
    onboard creates gets a git baseline so it is dispatchable; an existing
    non-git directory is left as it is (nothing of the operator's is
    committed unasked)."""
    from .cli_cmds import _managed_repo_slug, _project_id_from_repo, _project_root
    from .serve.registry import ensure_repo_baseline, project_setup_lock, setup_project_record
    from .serve.setup import DEFAULT_WORKSPACE_PACK, DEFAULT_WORKSPACE_PHASE

    if not path.strip():
        raise ValueError("name a work directory")
    repo = Path(path).expanduser().resolve()
    created = not repo.exists()
    repo.mkdir(parents=True, exist_ok=True)
    git = created or (repo / ".git").exists()
    if git:
        ensure_repo_baseline(repo)
    config = holder.current
    project_id = _project_id_from_repo(repo)
    # D47 review: the id is the folder's basename, so a different folder with
    # the same name re-points the stored project — setup leaves an unlock
    # behind when the binding is new (v132-F4).
    with project_setup_lock(store):
        project = setup_project_record(
            run_store=store,
            root=_project_root(config),
            project_id=project_id,
            name=project_id,
            strategy=None,
            phase=DEFAULT_WORKSPACE_PHASE,
            pack_name=DEFAULT_WORKSPACE_PACK,
            repo_path=str(repo),
            repo_slug=_managed_repo_slug(repo, config),
            template_names=[],
            seed_default_schedules=False,
        )
        return _record(
            _personal_home(holder),
            "workdir",
            {
                "path": str(repo),
                "project_id": project["project_id"],
                "created": created,
                "git": git,
                "verify_command": str(project["policy"].get("verify_command") or ""),
            },
        )


# --- ③ sandbox ---------------------------------------------------------------------


def rings_line(backend: str) -> str:
    """Which enforcement rings are live under this backend — exactly (D8, I12)."""
    base = "rings live: floor, locked baseline, capability layer"
    if backend == "none":
        return (
            f"{base} — no sandbox ring is counted on, so workers are treated as "
            "unsandboxed: `builder` asks on every write instead of allowing in-workspace"
        )
    if backend == "podman":
        return (
            f"{base}, sandbox (podman; a run that needs a domain allowlist uses the native backend)"
        )
    native = sandbox.availability().backend or "native"
    return f"{base}, sandbox ({native})"


def detect_sandboxes(
    probe: Callable[[str | None], sandbox.SandboxAvailability] = sandbox.availability,
) -> dict[str, Any]:
    """What this host can confine workers with. Docker is reported, not
    offered: skep has no docker backend (podman is the container one)."""

    def view(p: sandbox.SandboxAvailability) -> dict[str, Any]:
        detail = p.backend if p.usable else f"{p.reason}: {p.detail}"
        return {"usable": p.usable, "detail": detail}

    found = {
        "auto": view(probe(None)),
        "podman": (
            view(probe(sandbox.PODMAN))
            if shutil.which(sandbox.PODMAN)
            else {"usable": False, "detail": "podman is not on PATH"}
        ),
        "none": {"usable": True, "detail": "no sandbox ring"},
    }
    recommended = next(name for name in ("auto", "podman", "none") if found[name]["usable"])
    docker = shutil.which("docker") is not None
    return {
        "backends": found,
        "recommended": recommended,
        "docker": {
            "found": docker,
            "detail": "found, but skep has no docker backend — use podman or auto"
            if docker
            else "not on PATH",
        },
    }


def onboard_sandbox(
    holder: ConfigHolder,
    store: RunStore,
    *,
    backend: str,
    probe: Callable[[str | None], sandbox.SandboxAvailability] = sandbox.availability,
) -> dict[str, Any]:
    """③ Record the backend in the ``sandbox_backend`` setting. A backend
    this host cannot run is refused, naming the way out (I9)."""
    from .serve.actions import update_policy

    if backend == "docker":
        raise ValueError(
            "skep has no docker backend — choose auto (native: seatbelt / bubblewrap), "
            "podman, or none"
        )
    if backend not in SANDBOX_BACKENDS:
        raise ValueError(f"sandbox must be one of {', '.join(SANDBOX_BACKENDS)}")
    if backend != "none":
        found = probe(None if backend == "auto" else backend)
        if not found.usable:
            raise ValueError(
                f"{backend} is unusable here ({found.reason}: {found.detail}) — "
                "choose another backend, or none to say so"
            )
    update_policy(store, holder, {"sandbox_backend": backend})
    return _record(
        _personal_home(holder), "sandbox", {"backend": backend, "rings": rings_line(backend)}
    )


# --- ④ template ----------------------------------------------------------------------


def baseline_candidate(template: str, sandbox_backend: str | None) -> PolicyTemplate:
    """The layer onboard locks: the template, with every allowed write or
    edit turned into an ask when no sandbox ring is counted on (D8)."""
    layer = load_policy_template(template)
    if sandbox_backend != "none":
        return layer
    rules = [
        rule.model_copy(
            update={"verdict": "require_approval", "rule_id": f"{rule.rule_id}-unsandboxed"}
        )
        if rule.verdict == "allow" and rule.action in ("write", "edit")
        else rule
        for rule in layer.rules
    ]
    return layer.model_copy(update={"rules": rules})


def template_preview(holder: ConfigHolder, *, template: str) -> dict[str, Any]:
    """What ④ would lock: the explain table, its sha256, the diff against a
    locked baseline, and ``locked`` when exactly this layer is already the
    approved one (nothing to do)."""
    from .policy_baseline import layer_diff
    from .serve.actions import baseline_preview

    home = _personal_home(holder)
    backend = (_read(home / STATE_FILE).get("sandbox") or {}).get("backend")
    layer = baseline_candidate(template, backend)
    state = verify(holder.current.home)
    if isinstance(state, Ok) and sha256_of(state.layer) == sha256_of(layer):
        return {"locked": True, "sha256": sha256_of(layer), "template": layer.template}
    _layer, view = baseline_preview(
        holder.current.home, layer=layer, reapprove=not isinstance(state, NoBaseline)
    )
    view["locked"] = False
    view["template"] = layer.template
    view["tightened"] = layer_diff(load_policy_template(template), layer)
    view["tampered"] = isinstance(state, Tampered)
    return view


def onboard_template(
    holder: ConfigHolder,
    *,
    template: str,
    approved_by: str,
    expect_sha256: str | None = None,
) -> dict[str, Any]:
    """④ Lock the candidate as the baseline through O2's approve path."""
    from .serve.actions import approve_policy_baseline

    home = _personal_home(holder)
    view = template_preview(holder, template=template)
    if not view["locked"]:
        backend = (_read(home / STATE_FILE).get("sandbox") or {}).get("backend")
        approve_policy_baseline(
            holder.current.home,
            layer=baseline_candidate(template, backend),
            reapprove=not isinstance(verify(holder.current.home), NoBaseline),
            approved_by=approved_by,
            expect_sha256=expect_sha256,
        )
    return _record(home, "template", {"template": view["template"], "sha256": view["sha256"]})


# --- ④b work without asking (ADR 0061 D47) ---------------------------------------------

UNLOCKED_STATE = "unlocked"  # the answer's record in onboard.json — not a step


def unlocked_offer(holder: ConfigHolder, store: RunStore) -> dict[str, Any]:
    """Can onboard offer unlocked mode on the work directory's project? The
    offer is a question the operator answers once (I6: the per-project ramp,
    climbed explicitly) — never a default, and ``--yes`` never answers it.
    ``reason`` says why it cannot be offered (the save-time guard's words)."""
    from .projects import project_from_store
    from .serve.registry import unlocked_block_reason

    state = _read(_personal_home(holder) / STATE_FILE)
    workdir = state.get("workdir") or {}
    project = project_from_store(store, str(workdir.get("project_id") or ""))
    answers = (state.get(UNLOCKED_STATE) or {}).get("answers") or {}
    view: dict[str, Any] = {
        "path": workdir.get("path"),
        "project_id": workdir.get("project_id"),
        "on": project is not None and project.policy.get("unlocked") is True,
        # an answer holds for the folder it was given for, not the next one
        "asked": workdir.get("path") in answers,
        "declined": answers.get(workdir.get("path")) is False,
    }
    if project is None:
        reason: str | None = "register a work directory first (step ②)"
    elif other := sorted(
        f"{b.binding_kind} {b.binding_value}"
        for b in store.project_bindings(project.project_id)
        if (b.binding_kind, b.binding_value) != ("repo_path", workdir["path"])
    ):
        # D47 review: the unlock is the project's — it would cover every repo
        # the project binds, and the operator was shown one folder. A slug
        # too: the scheduler resolves slugs by basename (scheduler.py), so a
        # same-named folder elsewhere could inherit it.
        reason = (
            f"project {project.project_id} also binds {', '.join(other)}; decide for all of "
            f"it with skep project setup {shlex.quote(str(workdir['path']))} --unlocked"
        )
    elif onboard_status(holder.current.home)["steps"]["template"] is None:
        reason = "lock a policy baseline first (step ④)"
    elif (state.get("sandbox") or {}).get("backend") == "none":
        reason = "step ③ chose no sandbox, and unlocked runs are sandboxed"
    else:
        reason = unlocked_block_reason(store, {**project.policy, "unlocked": True})
    return {**view, "reason": reason, "offer": not view["on"] and reason is None}


def onboard_unlocked(
    holder: ConfigHolder, store: RunStore, *, on: bool, path: str | None
) -> dict[str, Any]:
    """Record the answer; ``on`` turns unlocked on for the work directory's
    project through project setup (the same write as
    ``skep project setup <dir> --unlocked``, under the same guard). ``path``
    is the folder the operator was shown: a work directory changed since is
    refused, so a yes never lands on a project nobody asked about."""
    from .serve.registry import project_setup_lock

    with project_setup_lock(store):  # v132-F3: checks and write, one lock
        return _onboard_unlocked(holder, store, on=on, path=path)


def _onboard_unlocked(
    holder: ConfigHolder, store: RunStore, *, on: bool, path: str | None
) -> dict[str, Any]:
    from .cli_cmds import _project_root
    from .projects import project_from_store
    from .serve.registry import setup_project_record

    offer = unlocked_offer(holder, store)
    if offer["path"] is None:
        raise ValueError(f"cannot work without asking yet: {offer['reason']}")
    if path != offer["path"]:
        raise ValueError(
            f"the work directory is now {offer['path']}, not {path}: reload and answer again"
        )
    if on and not offer["on"]:
        if offer["reason"] is not None:
            raise ValueError(f"cannot work without asking yet: {offer['reason']}")
        project = project_from_store(store, str(offer["project_id"]))
        assert project is not None  # no reason means the project loaded
        bound = store.project_for_binding("repo_path", str(offer["path"]))
        if bound is None or bound.project_id != project.project_id:
            raise ValueError(f"{path} is no longer project {project.project_id}: answer again")
        setup_project_record(
            run_store=store,
            root=_project_root(holder.current),
            project_id=project.project_id,
            name=project.name,
            strategy=project.strategy,
            phase=project.phase,
            pack_name=project.pack_name,
            repo_path=str(offer["path"]),  # the write binds the folder shown, nothing else
            policy_overrides={"unlocked": True},
            seed_default_schedules=False,
            seed_shell_commands=False,
        )
    home = _personal_home(holder)
    answers = (_read(home / STATE_FILE).get(UNLOCKED_STATE) or {}).get("answers") or {}
    value = on or offer["on"]
    record = _record(home, UNLOCKED_STATE, {"answers": {**answers, offer["path"]: value}})
    return {**record, "path": offer["path"], "on": value}


# --- ⑤ role ----------------------------------------------------------------------------


def onboard_role(holder: ConfigHolder, *, role: str) -> dict[str, Any]:
    """⑤ A role record (R1, ADR 0055: the three shipped ones bind 1:1 to the
    templates; a home's custom roles count too). ``first_run.json`` keeps its
    counters across re-runs."""
    from .roles import resolve_role

    role = resolve_role(role, holder.current.home).name
    path = _personal_home(holder) / FIRST_RUN_FILE
    current = _read(path)
    record = {
        "started_at": current.get("started_at") or _now(),
        "turns_seen": current.get("turns_seen", 0),
        "verdicts_seen": current.get("verdicts_seen", 0),
        "dismissed": current.get("dismissed", False),
        "role": role,
    }
    _write(path, record)
    return record


# --- --check -----------------------------------------------------------------------------


def _check(ok: bool, detail: str) -> dict[str, str]:
    return {"status": "ready" if ok else "blocked", "detail": detail}


def onboard_check(supervisor_home: Path) -> dict[str, Any]:
    """`skep onboard --check`: `skep doctor` plus the onboard rings and the
    egress-truth block (ADR 0051) whose last line is ``telemetry: none``.
    Read-only; ``ready`` only when every check is."""
    from ..status import build_status, format_doctor_report
    from .policy_baseline import BASELINE_TAMPERED, baseline_status, locked_layer
    from .policy_schema import SCOPES
    from .provider_hosts import configured_provider_hosts
    from .serve.settings import POLICY_ENGINE_V2_SCOPES, engine_v2_scopes_from

    home = supervisor_home.parent
    status = build_status(home)
    state = _read(home / STATE_FILE)
    checks: dict[str, dict[str, str]] = {}

    steps = onboard_status(supervisor_home)
    baseline = baseline_status(supervisor_home)
    if baseline["state"] == "ok" and steps["relock"]:
        backend_now = (state.get("sandbox") or {}).get("backend")
        checks["baseline"] = _check(
            False,
            f"{baseline['template']} locked under another sandbox — not what onboard locks "
            f"for {steps['relock']} with sandbox {backend_now}; run skep onboard to re-approve",
        )
    elif baseline["state"] == "ok":
        checks["baseline"] = _check(
            True, f"{baseline['template']} locked, sha256 {baseline['sha256'][:12]}"
        )
    elif baseline["state"] == "tampered":
        checks["baseline"] = _check(False, f"{BASELINE_TAMPERED}: {'; '.join(baseline['diff'])}")
    elif baseline["state"] == "reapprove":
        checks["baseline"] = _check(False, "schema changed — `skep policy approve --reapprove`")
    else:
        checks["baseline"] = _check(False, "none approved — run skep onboard")

    store = RunStore(supervisor_home / "supervisor.sqlite3")
    try:
        scopes = engine_v2_scopes_from(store.get_setting(POLICY_ENGINE_V2_SCOPES))
        backend = store.get_setting(SANDBOX_BACKEND)
        hosts = list(dict.fromkeys(configured_provider_hosts(store, home)))  # L2: once each
    finally:
        store.close()
    # O5: a fresh home seeds every scope, so anything less is a narrowed switch.
    missing = [scope for scope in SCOPES if scope not in scopes]
    # L2: ADR 0059 has no v1/v2 engine split; the switch only picks the scopes
    # a home with no approved baseline holds to "ask" (policy_layers.baseline_guard)
    checks["policy scopes"] = _check(
        not missing,
        f"{len(scopes)}/{len(SCOPES)} scopes — the baseline governs these"
        + (f"; switched off (no baseline guard): {', '.join(missing)}" if missing else ""),
    )

    if backend not in SANDBOX_BACKENDS:
        checks["sandbox"] = _check(False, "not chosen — run skep onboard")
    elif backend == "none":
        checks["sandbox"] = _check(True, f"none — {rings_line('none')}")
    else:
        found = sandbox.availability(None if backend == "auto" else str(backend))
        checks["sandbox"] = _check(
            found.usable,
            rings_line(str(backend)) if found.usable else f"{backend}: {found.reason}",
        )

    workdir = (state.get("workdir") or {}).get("path")
    if not workdir:
        checks["workdir"] = _check(False, "not set — run skep onboard")
    else:
        writable = Path(workdir).is_dir() and os.access(workdir, os.W_OK)
        checks["workdir"] = _check(writable, f"{workdir} {'is' if writable else 'is NOT'} writable")

    provider = status["required"].get("provider", {"status": "blocked", "detail": "no profile"})
    checks["model"] = _check(provider.get("status") == "ready", str(provider.get("detail")))

    # doctor honesty: the model step reads the model check above, never a
    # second source (onboard.json) that can disagree with it
    pending = [
        step
        for step in STEPS
        if (
            checks["model"]["status"] != "ready"
            if step == "model"
            else steps["steps"][step] is None
        )
    ]
    checks["steps"] = _check(
        not pending, "all five complete" if not pending else f"pending: {', '.join(pending)}"
    )

    model = state.get("model") or {}
    if model.get("provider") == "mock":
        model_egress = str(model["egress"])
    elif not hosts:
        model_egress = "no provider configured — nothing is sent"
    elif all(host.rpartition(":")[0].strip("[]") in _LOOPBACK for host in hosts):  # host:port
        model_egress = f"LOCAL ({', '.join(hosts)}) — nothing leaves this machine"
    else:
        model_egress = (
            f"CLOUD — every assistant/worker prompt leaves this machine for {', '.join(hosts)}"
        )
    locked = locked_layer(supervisor_home)
    network = (locked.settings.get("default_network") if locked else None) or []
    workers_egress = (
        f"{', '.join(network)} allowed by the baseline; every other host asks"
        if network
        else "no host allowed by default; every connection asks"
    )
    egress = [f"model: {model_egress}", f"workers: {workers_egress}", "telemetry: none"]

    ready = status["overall"] == "ready" and all(c["status"] == "ready" for c in checks.values())
    lines = [format_doctor_report(status).rstrip(), "", "Onboard checks:"]
    lines += [f"- {name}: {c['status']} - {c['detail']}" for name, c in checks.items()]
    lines += ["", "Egress truth:", *egress]
    return {"ready": ready, "checks": checks, "egress": egress, "report": "\n".join(lines) + "\n"}


# --- first-run transparency (O4, ADR 0056 D6) ----------------------------------------------

OFFER_TURNS = 10
OFFER_VERDICTS = 25
_VERDICT_WORD = {"allow": "allowed", "allow_with_constraints": "allowed", "deny": "denied"}


def transparency_active(personal_home: Path) -> bool:
    """While ``first_run.json`` exists and is not dismissed, every verdict
    card carries its ``why``. A home that never onboarded has no file, so
    nothing changes for it. O4-F1: nor for one whose counters are malformed —
    it reads as absent, exactly like a malformed file."""
    record = _read(personal_home / FIRST_RUN_FILE)
    return bool(record) and not record.get("dismissed") and _counters(record) is not None


def _counters(record: dict[str, Any]) -> tuple[int, int] | None:
    """``(turns_seen, verdicts_seen)``, or None when either is not a plain int."""
    turns, verdicts = record.get("turns_seen", 0), record.get("verdicts_seen", 0)
    if type(turns) is not int or type(verdicts) is not int:
        return None
    return turns, verdicts


def verdict_why(
    decision: dict[str, Any] | None, *, tool: str | None = None, args: Any = None
) -> str:
    """The one-line ``why`` under a verdict card, read off the decision the
    card already carries (I8): the rule and the layer it came from, then the
    reason code. No decision: a tool that always cards says why it does
    (L8, ``tools.ALWAYS_CARDS``; a research run names its question and
    sources from ``args``); any other asked because no rule named it."""
    if not decision:
        from .serve.tools import ALWAYS_CARDS

        if tool == "start_research":
            safe = args if isinstance(args, dict) else {}
            hosts = ", ".join(str(h) for h in safe.get("source_allowlist") or []) or "no host"
            return (
                f"asked: researching {str(safe.get('question') or '')!r} is a sandboxed run "
                f"that fetches from {hosts}, and a person starts every research run"
            )
        if tool == "remember":  # v126-F12: it files in-turn; it never asks
            return (
                "allowed: remember files an inert proposal at once — nothing is "
                "remembered until you approve that proposal"
            )
        if tool in ALWAYS_CARDS:
            return f"asked: {tool} always asks — {ALWAYS_CARDS[tool]}"
        return "asked: no policy rule covers this tool, so it asks by default"
    if decision.get("decided_by") == "default/unmatched":  # ADR 0059: decide() matched nothing
        return "asked: no policy rule covers this, so it asks"
    layer_word = str(decision.get("decided_by") or "").partition("/")[0]
    if layer_word in ("tier", "taint") and decision.get("detail"):
        return f"asked: {decision['detail']}"  # L6: its plain-words reason, not code words
    word = _VERDICT_WORD.get(str(decision.get("verdict")), "asked")
    reason = str(decision.get("reason") or "")
    decided_by = str(decision.get("decided_by") or "")
    layer, _, rule = decided_by.rpartition("/")
    if rule and layer:
        line = f"{word}: rule {rule} in the {layer} layer"
    elif rule:
        line = f"{word}: rule {rule}"
    else:
        line = f"{word}: {reason or 'policy'}"
        reason = ""
    if reason and reason != rule:
        line += f" — {reason}"
    return line


def note_turn(personal_home: Path, tally: dict[str, int]) -> dict[str, Any]:
    """Count one turn and its verdicts in ``first_run.json``; return the
    tally line, plus the offer to turn it down on the turn that first crosses
    10 turns or 25 verdicts."""
    path = personal_home / FIRST_RUN_FILE
    # ponytail: unlocked read-modify-write; two concurrent turns can drop one count
    record = _read(path)
    # O4-F1: corrupted after the turn's transparency_active read — start over
    # rather than kill the chat stream mid-turn.
    turns, verdicts = _counters(record) or (0, 0)
    record["turns_seen"], record["verdicts_seen"] = turns + 1, verdicts + sum(tally.values())
    _write(path, record)
    view: dict[str, Any] = {
        **tally,
        "line": (
            f"skep: {tally['allowed']} allowed · {tally['asked']} asked · {tally['denied']} denied"
            " — skep policy why for any of them"
        ),
    }
    before = turns >= OFFER_TURNS or verdicts >= OFFER_VERDICTS
    after = record["turns_seen"] >= OFFER_TURNS or record["verdicts_seen"] >= OFFER_VERDICTS
    if after and not before:
        view["offer"] = (
            f"skep: {record['turns_seen']} turns and {record['verdicts_seen']} verdicts "
            "with why-lines on — turn them down with skep onboard --quiet "
            "(--verbose brings them back)"
        )
    return view


def onboard_transparency(supervisor_home: Path, *, verbose: bool) -> dict[str, Any]:
    """`skep onboard --quiet | --verbose` and ``POST /api/onboard/transparency``:
    flip the why-lines off or on. The counters are kept; ``--verbose`` on a
    home that never onboarded starts the record. Operator faces only — the
    model is never handed the switch that hides its own verdicts (I6)."""
    path = supervisor_home.parent / FIRST_RUN_FILE
    record = _read(path)
    record.setdefault("started_at", _now())
    record.setdefault("turns_seen", 0)
    record.setdefault("verdicts_seen", 0)
    record["dismissed"] = not verbose
    _write(path, record)
    return record
