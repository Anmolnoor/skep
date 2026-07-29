"""v4: the learned-skill lifecycle — propose -> test -> approve/reject — and its CLI.

This module is the **governance** that gives v4 its substance. The generalizer
(``skills.py``) only drafts; nothing it produces can run until it has passed two
independent gates:

  1. **The test gate (``skill test``).** A draft is instantiated against a real
     repo and dispatched through the *same* ``run_task`` spine as any task. It is
     promoted to ``tested`` only if the run completes AND the supervisor's own G10
     re-verification confirms it. A failed test is auto-rejected (``auto:test-gate``)
     and can never be approved — fail-closed.
  2. **The human gate (``skill approve``).** Only a person can move a ``tested``
     candidate into the registry. A candidate NEVER self-promotes.

On approval the recipe joins the **same** v3.5 ``templates`` library (tagged
``provenance="learned"``) and is run/scheduled identically to a hand-authored
template. Every decision — auto-rejection, human approval, human rejection — is
recorded in the existing audit store (the approval queue, anchored to the evidence
run, plus the candidate's own decision fields).
"""

from __future__ import annotations

import argparse
import dataclasses
import getpass
from dataclasses import dataclass
from pathlib import Path

from skep.worker_contract import CodingWorkerTask, TaskState

from .config import SupervisorConfig
from .dispatch import run_task
from .scheduler import now_ts
from .skills import (
    APPROVED,
    DEFAULT_MAX_PARAMS,
    DEFAULT_MIN_OCCURRENCES,
    DRAFT,
    REJECTED,
    TEST_GATE_ACTOR,
    TESTED,
    RunShape,
    SkillCandidate,
    candidate_signature,
    draft_candidates,
    generate,
    promote_to_template,
)
from .store import RunStore
from .templates import TemplateError, instantiate


class SkillError(ValueError):
    """A lifecycle/gate violation, surfaced with a doctor-style CLI message."""


# -- the pipeline (testable without argparse) ---------------------------------


def load_run_shapes(store: RunStore, audit_dir: Path, *, limit: int = 1000) -> list[RunShape]:
    """Reconstruct the task shape of every completed, G10-confirmed run.

    Only *successful, independently re-verified* runs feed the generalizer — the
    same evidence bar the rest of the system trusts. The run record lacks the caste
    / network / budget, so those come from the run's audited ``task.json``.
    """
    shapes: list[RunShape] = []
    for run in store.recent_runs(limit):
        if run.state != TaskState.COMPLETED.value:
            continue
        reverify = store.reverification_for(run.task_id)
        if reverify is None or not reverify.confirmed:
            continue
        task_path = audit_dir / run.task_id / "task.json"
        if not task_path.is_file():
            continue
        try:
            task = CodingWorkerTask.model_validate_json(task_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        shapes.append(
            RunShape(
                task_id=run.task_id,
                worker_kind=task.worker_kind,
                instructions=task.instructions,
                network=tuple(task.permissions.network),
                env_allowlist=tuple(task.permissions.env_allowlist),
                wall_clock_seconds=task.budget.wall_clock_seconds,
                max_iterations=task.budget.max_iterations,
                max_actions=task.budget.max_actions,
                max_provider_calls=task.budget.max_provider_calls,
            )
        )
    return shapes


def propose(
    store: RunStore,
    audit_dir: Path,
    *,
    min_occurrences: int = DEFAULT_MIN_OCCURRENCES,
    max_params: int = DEFAULT_MAX_PARAMS,
    now: str | None = None,
) -> list[SkillCandidate]:
    """Generalize successful runs into fresh draft candidates and persist them.

    Idempotent: a recipe already drafted, already approved into the registry, or
    previously rejected is not re-proposed (dedup by content signature), so there is
    no learn-it-again loop.
    """
    shapes = load_run_shapes(store, audit_dir)
    generated = generate(shapes, min_occurrences=min_occurrences, max_params=max_params)
    known = {candidate_signature(c.template) for c in store.list_candidates()}
    known |= {candidate_signature(t) for t in store.list_templates()}
    drafts = draft_candidates(
        generated, known_signatures=frozenset(known), created_at=now or now_ts()
    )
    for draft in drafts:
        store.add_candidate(draft)
    return drafts


@dataclass(frozen=True)
class TestResult:
    passed: bool
    task_id: str
    state: str
    confirmed: bool
    detail: str


def _evidence_note(candidate: SkillCandidate) -> str:
    sources = ", ".join(candidate.source_task_ids) or "(none)"
    return (
        f"generalized from {candidate.occurrences} run(s): {sources}; "
        f"test {candidate.test_task_id} completed + G10-confirmed"
    )


def _record_decision(
    store: RunStore,
    *,
    anchor_task_id: str | None,
    reason: str,
    approved: bool,
    actor: str,
    note: str,
) -> None:
    """Mirror patch-approval: record the decision in the approval queue, anchored to
    the evidence run. Skipped only when there is no run to anchor to (rejecting an
    untested draft) — the candidate row is the durable record in that case."""
    if anchor_task_id is None:
        return
    review_id = store.enqueue_approval(anchor_task_id, action="promote_skill", reason=reason)
    store.resolve_approval(review_id, approved=approved, actor=actor, note=note)


def run_candidate_test(
    store: RunStore,
    config: SupervisorConfig,
    name: str,
    *,
    repo: Path,
    params: dict[str, str],
    ref: str | None = None,
    now: str | None = None,
) -> tuple[SkillCandidate, TestResult]:
    """Run a draft against a real repo; promote to ``tested`` iff it G10-confirms.

    A non-passing test is auto-rejected by ``auto:test-gate`` — the candidate can
    never be approved (approval requires ``tested``). This is the fail-closed gate
    that proves a candidate which fails its test NEVER enters the registry.
    """
    candidate = store.get_candidate(name)
    if candidate is None:
        raise SkillError(f"no skill candidate named {name!r} (see: skep skill list)")
    if candidate.status != DRAFT:
        raise SkillError(f"candidate {name!r} is {candidate.status!r}; only a draft can be tested")
    try:
        instance = instantiate(candidate.template, params, repo=str(repo), ref=ref)
    except (TemplateError, ValueError) as exc:
        raise SkillError(str(exc)) from exc

    outcome = run_task(
        Path(instance.repo),
        instance.instructions,
        config=config,
        worker_kind=instance.worker_kind,
        permissions=instance.permissions,
        budget=instance.budget,
        ref=instance.ref,
        store=store,
    )
    reverify = store.reverification_for(outcome.record.task_id)
    confirmed = reverify is not None and reverify.confirmed
    passed = outcome.record.state == TaskState.COMPLETED.value and confirmed
    detail = (
        f"test run {outcome.record.task_id}: state={outcome.record.state}, "
        f"re-verified={'confirmed' if confirmed else 'NOT confirmed'}"
    )
    result = TestResult(
        passed=passed,
        task_id=outcome.record.task_id,
        state=outcome.record.state,
        confirmed=confirmed,
        detail=detail,
    )
    moment = now or now_ts()
    if passed:
        updated = dataclasses.replace(
            candidate, status=TESTED, test_task_id=result.task_id, test_outcome="passed"
        )
    else:
        # Fail-closed: a failed test is a terminal auto-rejection, not a retry.
        updated = dataclasses.replace(
            candidate,
            status=REJECTED,
            test_task_id=result.task_id,
            test_outcome="failed",
            decided_by=TEST_GATE_ACTOR,
            decided_at=moment,
            decision_note=detail,
        )
        _record_decision(
            store,
            anchor_task_id=result.task_id,
            reason=f"skill {name!r} test gate: {detail}",
            approved=False,
            actor=TEST_GATE_ACTOR,
            note=detail,
        )
    store.add_candidate(updated)
    return updated, result


def approve(
    store: RunStore,
    name: str,
    *,
    actor: str,
    note: str | None = None,
    registry_name: str | None = None,
    now: str | None = None,
) -> tuple[SkillCandidate, str]:
    """The human gate: move a ``tested`` candidate into the registry. Returns the
    candidate and the registry name it landed under.

    Refuses anything not ``tested`` (a draft must pass the gate first; a rejected
    candidate is terminal). Refuses to clobber an existing template name.
    """
    candidate = store.get_candidate(name)
    if candidate is None:
        raise SkillError(f"no skill candidate named {name!r} (see: skep skill list)")
    if candidate.status == APPROVED:
        raise SkillError(
            f"candidate {name!r} is already approved as template {candidate.registry_name!r}"
        )
    if candidate.status == REJECTED:
        by = candidate.decided_by or "?"
        raise SkillError(f"candidate {name!r} was rejected (by {by}); it cannot enter the registry")
    if candidate.status != TESTED and not (
        candidate.status == DRAFT and candidate.template.provenance == "conversation"
    ):
        # v53-F1 (ADR 0029): a conversation draft has no runnable worker test
        # — the v51-F4 create_skill reasoning: generated chat procedures get
        # the HUMAN gate, and this approve is exactly that gate. Learned
        # worker recipes still must pass their test first.
        raise SkillError(
            f"candidate {name!r} is {candidate.status!r} — test it first: "
            f"skep skill test {name} <repo>"
        )

    target = registry_name or candidate.name
    if store.get_template(target) is not None:
        raise SkillError(
            f"a template named {target!r} already exists; choose another with --as NAME"
        )
    moment = now or now_ts()
    template = promote_to_template(candidate, name=target, created_at=moment)
    store.add_template(template)

    decision_note = note or f"approved into registry as {target!r}"
    _record_decision(
        store,
        anchor_task_id=candidate.test_task_id,
        reason=f"promote learned skill {name!r}: {_evidence_note(candidate)}",
        approved=True,
        actor=actor,
        note=decision_note,
    )
    updated = dataclasses.replace(
        candidate,
        status=APPROVED,
        decided_by=actor,
        decided_at=moment,
        decision_note=decision_note,
        registry_name=target,
    )
    store.add_candidate(updated)
    return updated, target


def reject(
    store: RunStore,
    name: str,
    *,
    actor: str,
    note: str | None = None,
    now: str | None = None,
) -> SkillCandidate:
    """The human gate's other half: deny a candidate so it never enters the registry."""
    candidate = store.get_candidate(name)
    if candidate is None:
        raise SkillError(f"no skill candidate named {name!r} (see: skep skill list)")
    if candidate.status == APPROVED:
        raise SkillError(
            f"candidate {name!r} is already approved as template "
            f"{candidate.registry_name!r}; remove the template to retract it"
        )
    if candidate.status == REJECTED:
        raise SkillError(f"candidate {name!r} is already rejected")

    moment = now or now_ts()
    decision_note = note or "rejected by human review"
    _record_decision(
        store,
        anchor_task_id=candidate.test_task_id,
        reason=f"reject learned skill {name!r}",
        approved=False,
        actor=actor,
        note=decision_note,
    )
    updated = dataclasses.replace(
        candidate,
        status=REJECTED,
        decided_by=actor,
        decided_at=moment,
        decision_note=decision_note,
    )
    store.add_candidate(updated)
    return updated


# -- CLI commands -------------------------------------------------------------


def cmd_skill_propose(args: argparse.Namespace) -> int:
    from .cli_cmds import build_config

    config = build_config(args.home, None)
    if not config.db_path.is_file():
        print("no runs yet — nothing to learn from")
        print('next: skep run <repo> "..."  (skills are generalized from successful runs)')
        return 0
    store = RunStore(config.db_path)
    try:
        drafts = propose(
            store,
            config.audit_dir,
            min_occurrences=args.min_occurrences,
            max_params=args.max_params,
        )
    finally:
        store.close()
    if not drafts:
        print("no new skills proposed")
        print(
            "  (need >= "
            f"{args.min_occurrences} successful, re-verified runs that share a shape; "
            "already-known recipes are skipped)"
        )
        return 0
    print(f"proposed {len(drafts)} draft skill candidate(s):")
    for candidate in drafts:
        sources = ", ".join(t[:8] for t in candidate.source_task_ids)
        print(f"\n  {candidate.name}  [draft]  ({candidate.occurrences} runs: {sources})")
        print(f"    caste:        {candidate.template.worker_kind}")
        print(f"    instructions: {candidate.template.instructions}")
        params = ", ".join(p.name for p in candidate.template.params) or "-"
        print(f"    parameters:   {params}")
        print(f"    next:         skep skill test {candidate.name} <repo> --param ...")
    return 0


def cmd_skill_list(args: argparse.Namespace) -> int:
    from .cli_cmds import build_config

    config = build_config(args.home, None)
    if not config.db_path.is_file():
        print("no skill candidates yet")
        return 0
    store = RunStore(config.db_path)
    try:
        candidates = store.list_candidates()
    finally:
        store.close()
    if not candidates:
        print("no skill candidates yet")
        print("next: skep skill propose  (generalize successful runs into candidates)")
        return 0
    print(f"{'name':<26} {'status':<9} {'caste':<7} {'occ':<4} {'test':<8} params")
    for c in candidates:
        params = ", ".join(p.name for p in c.template.params) or "-"
        print(
            f"{c.name[:25]:<26} {c.status:<9} {c.template.worker_kind:<7} "
            f"{c.occurrences:<4} {(c.test_outcome or '-'):<8} {params}"
        )
    print("\napproved skills live in the registry: skep template list")
    return 0


def cmd_skill_show(args: argparse.Namespace) -> int:
    from .cli_cmds import _err, build_config

    config = build_config(args.home, None)
    store = RunStore(config.db_path) if config.db_path.is_file() else None
    candidate = store.get_candidate(args.name) if store is not None else None
    if store is not None:
        store.close()
    if candidate is None:
        return _err(f"no skill candidate named {args.name!r}.", next_command="skep skill list")
    print(f"skill candidate {candidate.name}")
    print(f"  status:       {candidate.status}")
    print(f"  caste:        {candidate.template.worker_kind}")
    print(f"  provenance:   {candidate.template.provenance}")
    print(f"  occurrences:  {candidate.occurrences}")
    print(f"  source runs:  {', '.join(candidate.source_task_ids) or '-'}")
    print(f"  network:      {', '.join(candidate.template.network) or '(deny all outbound)'}")
    print("  instructions:")
    for line in candidate.template.instructions.splitlines() or [""]:
        print(f"    {line}")
    print("  parameters:")
    for p in candidate.template.params:
        print(f"    {p.name}  (required)")
    if not candidate.template.params:
        print("    (none)")
    if candidate.test_task_id is not None:
        print(f"  test run:     {candidate.test_task_id} -> {candidate.test_outcome}")
    if candidate.decided_by is not None:
        print(
            f"  decision:     {candidate.status} by {candidate.decided_by} "
            f"at {candidate.decided_at or '-'}"
        )
        if candidate.decision_note:
            print(f"                {candidate.decision_note}")
    if candidate.registry_name is not None:
        print(f"  registry:     template {candidate.registry_name!r}")
    if candidate.status == DRAFT:
        print(f"  next:         skep skill test {candidate.name} <repo> --param ...")
    elif candidate.status == TESTED:
        print(f"  next:         skep skill approve {candidate.name}   (or reject)")
    elif candidate.status == APPROVED:
        print(f"  next:         skep run --template {candidate.registry_name} <repo> --param ...")
    return 0


def cmd_skill_test(args: argparse.Namespace) -> int:
    from .cli_cmds import _err, _parse_params, build_config

    config = build_config(args.home, args.worker_cmd)
    if not config.db_path.is_file():
        return _err("no skill candidates yet.", next_command="skep skill propose")
    repo = Path(args.repo).expanduser().resolve()
    if not (repo / ".git").exists():
        return _err(f"{repo} is not a git repository.", next_command="point at a git repo")
    try:
        params = _parse_params(args.param)
    except ValueError as exc:
        return _err(str(exc))
    store = RunStore(config.db_path)
    try:
        candidate, result = run_candidate_test(
            store, config, args.name, repo=repo, params=params, ref=args.ref
        )
    except SkillError as exc:
        return _err(str(exc))
    finally:
        store.close()
    print(f"tested {args.name!r}: {result.detail}")
    if result.passed:
        print(f"  -> {candidate.status}  (passed the G10 test gate)")
        print(f"  next: skep skill approve {args.name}   # human approval into the registry")
        return 0
    print(f"  -> {candidate.status}  (auto-rejected by {TEST_GATE_ACTOR}; cannot be approved)")
    return 3


def cmd_skill_approve(args: argparse.Namespace) -> int:
    from .cli_cmds import _err, build_config

    config = build_config(args.home, None)
    if not config.db_path.is_file():
        return _err("no skill candidates yet.", next_command="skep skill propose")
    actor = args.actor or getpass.getuser()
    store = RunStore(config.db_path)
    try:
        candidate, target = approve(
            store, args.name, actor=actor, note=args.note, registry_name=args.as_name
        )
    except SkillError as exc:
        return _err(str(exc))
    finally:
        store.close()
    print(f"approved: skill {args.name!r} promoted into the registry as template {target!r}")
    print(f"  by {actor}; evidence: {_evidence_note(candidate)}")
    required = "".join(f" --param {p.name}=..." for p in candidate.template.params)
    print(f"  run it:      skep run --template {target}{required}")
    print(f"  schedule it: skep schedule add JOB <repo> --template {target} --every 1d{required}")
    return 0


def cmd_skill_reject(args: argparse.Namespace) -> int:
    from .cli_cmds import _err, build_config

    config = build_config(args.home, None)
    if not config.db_path.is_file():
        return _err("no skill candidates yet.", next_command="skep skill propose")
    actor = args.actor or getpass.getuser()
    store = RunStore(config.db_path)
    try:
        reject(store, args.name, actor=actor, note=args.note)
    except SkillError as exc:
        return _err(str(exc))
    finally:
        store.close()
    print(f"rejected: skill {args.name!r} (by {actor}); it will not enter the registry")
    return 0


def cmd_skill_export(args: argparse.Namespace) -> int:
    import json

    from .cli_cmds import _err, build_config
    from .skill_bundle import bundle_skill, sign_bundle, skill_signing_key

    config = build_config(args.home, None)
    if not config.db_path.is_file():
        return _err("no registry yet.", next_command="skep template list")
    store = RunStore(config.db_path)
    try:
        template = store.get_template(args.name)
    finally:
        store.close()
    if template is None:
        return _err(f"no skill/template named {args.name!r}.", next_command="skep template list")
    signed = sign_bundle(bundle_skill(template), skill_signing_key(config.home))
    text = json.dumps(signed, indent=2, sort_keys=True) + "\n"
    if args.out:
        out = Path(args.out)
        out.write_text(text, encoding="utf-8")
        print(f"exported: skill {args.name!r} -> {out} (signed, key {signed['key_id']})")
    else:
        print(text, end="")
    return 0


def cmd_skill_import(args: argparse.Namespace) -> int:
    import json

    from .cli_cmds import _err, build_config
    from .skill_bundle import (
        grants_summary,
        skill_from_bundle,
        skill_grants,
        skill_signing_key,
        verify_bundle,
    )
    from .templates import TemplateError, validate_template

    path = Path(args.file)
    if not path.is_file():
        return _err(f"no bundle file at {path}.")
    try:
        bundle = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return _err(f"cannot read bundle: {exc}")
    try:
        template = skill_from_bundle(bundle)
        validate_template(template)
    except (ValueError, TemplateError) as exc:
        return _err(f"invalid skill bundle: {exc}")

    config = build_config(args.home, None)
    verification = verify_bundle(bundle, skill_signing_key(config.home))
    grants = skill_grants(template)

    # The human ALWAYS sees the full grant surface before anything is admitted.
    print(f"skill:        {template.name}  ({template.worker_kind})")
    print(f"signature:    {verification}")
    print(f"grants:       {grants_summary(grants)}")
    if grants["dangerous"]:
        print("  ^ this skill carries capability grants — review them before trusting it")

    if verification == "tampered":
        return _err(
            "refusing to import: this bundle claims your signing key but its signature "
            "is invalid — it was modified after you signed it."
        )
    if not args.approve:
        note = "" if verification == "verified" else "unverified signature — "
        print(
            f"\n{note}nothing imported. Review the grants above, then re-run with "
            "--approve to admit this skill into the registry (it will not run until "
            "you dispatch it)."
        )
        return 0

    import dataclasses

    registry_name = args.as_name or template.name
    imported = dataclasses.replace(template, name=registry_name, provenance="imported")
    validate_template(imported)
    store = RunStore(config.db_path)
    try:
        if store.get_template(registry_name) is not None:
            return _err(
                f"a skill named {registry_name!r} already exists; pass --as NAME to import "
                "under a different name (no silent overwrite)."
            )
        store.add_template(imported)
    finally:
        store.close()
    banner = "" if verification == "verified" else " (UNVERIFIED SIGNATURE — trust the source)"
    print(f"\nimported: skill {registry_name!r} added to the registry{banner}")
    print(f"  run it: skep run --template {registry_name}")
    return 0


def cmd_skill_import_md(args: argparse.Namespace) -> int:
    """v44-F6: import a Hermes-style SKILL.md pack through the v31 grant gate."""
    from .cli_cmds import _err, build_config
    from .skill_bundle import grants_summary, skill_grants
    from .skill_md import parse_skill_md, template_from_skill_md
    from .templates import TemplateError

    directory = Path(args.directory).expanduser()
    try:
        pack = parse_skill_md(directory)
        template = template_from_skill_md(
            pack, allow_scripts=tuple(args.allow_script or ())
        )
    except (ValueError, TemplateError, OSError) as exc:
        return _err(f"invalid SKILL.md pack: {exc}")

    grants = skill_grants(template)
    granted = {" ".join(command) for command in grants["shell_commands"]}
    print(f"skill:        {template.name}  ({template.worker_kind})")
    print(f"description:  {template.description}")
    print(f"grants:       {grants_summary(grants)}")
    for script in pack.scripts_found:
        mark = "GRANTED" if any(script in item for item in granted) else "not granted"
        print(f"  script:     {script}  [{mark}]")
    if pack.scripts_found and not granted:
        print(
            "  ^ shipped scripts grant NOTHING by themselves; re-run with "
            "--allow-script '<command>' for each one the skill may run"
        )

    if not args.approve:
        print(
            "\nnothing imported. Review the grants above, then re-run with --approve "
            "to admit this skill into the registry (it will not run until you "
            "dispatch it)."
        )
        return 0

    if pack.scripts_found and granted:
        # v85-F3: a pack whose scripts will RUN is a package — it walks the
        # v17 ladder (draft -> trial -> human approval -> active) instead of
        # jumping straight into the registry.
        from .skill_packs import SkillPackError, draft_pack

        config = build_config(args.home, None)
        store = RunStore(config.db_path)
        try:
            record = draft_pack(
                store, directory, grants=tuple(args.allow_script or ())
            )
        except SkillPackError as exc:
            return _err(str(exc))
        finally:
            store.close()
        print(f"\ndrafted: skill pack {record.pack_id!r} (state: {record.state})")
        print(
            f"  promote it: skep skill promote {record.pack_id}  "
            "(syntax trial + activation; the typed command is the approval)"
        )
        return 0

    registry_name = args.as_name or template.name
    imported = dataclasses.replace(template, name=registry_name)
    config = build_config(args.home, None)
    store = RunStore(config.db_path)
    try:
        if store.get_template(registry_name) is not None:
            return _err(
                f"a skill named {registry_name!r} already exists; pass --as NAME to "
                "import under a different name (no silent overwrite)."
            )
        store.add_template(imported)
    finally:
        store.close()
    print(f"\nimported: skill {registry_name!r} added to the registry")
    print(f"  run it: skep run --template {registry_name}")
    return 0


def cmd_skill_seed(args: argparse.Namespace) -> int:
    """v83-F12 (ADR 0043): sync the shipped seed shelf into the registry."""
    from .cli_cmds import build_config
    from .seed_skills import load_seed_skills

    config = build_config(args.home, None)
    store = RunStore(config.db_path)
    try:
        result = load_seed_skills(store)
    finally:
        store.close()
    for name in result["loaded"]:
        print(f"loaded:  {name}")
    for line in result["skipped"]:
        print(f"skipped: {line}")
    if result["existing"]:
        print(f"kept:    {result['existing']} existing (the operator's copy wins)")
    if not result["loaded"] and not result["skipped"] and not result["existing"]:
        print("no seeds found")
    return 0


def cmd_skill_promote(args: argparse.Namespace) -> int:
    """v85-F3: drive a drafted pack through the v17 ladder — the typed
    command IS the human action (I7)."""
    import shlex

    from .cli_cmds import _err, build_config
    from .skill_packs import SkillPackError, promote_pack

    config = build_config(args.home, None)
    store = RunStore(config.db_path)
    try:
        try:
            record, template = promote_pack(
                store,
                config,
                args.pack_id,
                extra_grants=tuple(args.allow_script or ()),
                human_action=True,
            )
        except SkillPackError as exc:
            return _err(str(exc))
    finally:
        store.close()
    if template is None:
        print(f"pack {record.pack_id!r} is already active")
        return 0
    trial = record.trial or {}
    for result in trial.get("scripts", []):
        mark = "ok" if result["ok"] else "FAIL"
        print(f"trial:  {result['script']}  [{result['check']}: {mark}]")
    # v100-F5 (R13): say which evidence the promotion rests on. A syntax-only
    # trial says so rather than letting 'tested' imply behaviour (I8).
    if trial.get("level") == "self_test":
        print(f"trial:  self_test [ok] — {trial.get('command')}")
    elif trial:
        print("trial:  syntax only — this pack declares no self_test")
    for command in template.shell_allowlist:
        print(f"grant:  {shlex.join(command)}")
    print(f"active: skill pack {record.pack_id!r}")
    print(f"  run it: skep run --template {record.pack_id}")
    return 0


def cmd_skill_packs(args: argparse.Namespace) -> int:
    """v85-F3: the pack ledger — every record and its ladder state."""
    from .cli_cmds import build_config
    from .skill_packs import load_packs

    config = build_config(args.home, None)
    store = RunStore(config.db_path)
    try:
        packs = load_packs(store)
    finally:
        store.close()
    if not packs:
        print(
            "no skill packs. `skep skill import-md DIR --allow-script '<cmd>' "
            "--approve` drafts one."
        )
        return 0
    for record in sorted(packs.values(), key=lambda r: r.pack_id):
        grants = f"  grants: {len(record.grants)}" if record.grants else ""
        print(f"{record.pack_id}  [{record.state}]  {record.description}{grants}")
    return 0


def cmd_skill_shelf(args: argparse.Namespace) -> int:
    """v85-F2: register/unregister external Agent Skills shelves
    (~/.claude/skills/ convention); no args lists them."""
    from .cli_cmds import _err, build_config
    from .seed_skills import (
        EXTERNAL_PROVENANCE,
        add_skill_shelf,
        load_seed_skills,
        remove_skill_shelf,
        skill_shelves,
    )

    config = build_config(args.home, None)
    store = RunStore(config.db_path)
    try:
        action = args.shelf_action
        if action is None:
            shelves = skill_shelves(store)
            if not shelves:
                print(
                    "no external shelves registered.\n"
                    "  add one: skep skill shelf add ~/.claude/skills"
                )
                return 0
            for entry in shelves:
                print(entry)
            return 0
        if not args.path:
            return _err(f"skill shelf {action} needs a PATH")
        path = Path(args.path).expanduser().resolve()
        if action == "remove":
            remove_skill_shelf(store, path)
            print(
                f"removed shelf: {path}\n"
                "  already-loaded skills stay in the registry; delete them "
                "individually if unwanted"
            )
            return 0
        try:
            add_skill_shelf(store, path)
        except ValueError as exc:
            return _err(str(exc))
        result = load_seed_skills(store, root=path, provenance=EXTERNAL_PROVENANCE)
        print(f"registered shelf: {path} (synced now and at every serve start)")
        for name in result["loaded"]:
            print(f"loaded:  {name}")
        for name in result.get("drafted", ()):
            print(
                f"drafted: {name} (ships scripts — promote it: skep skill "
                f"promote {name})"
            )
        for line in result["skipped"]:
            print(f"skipped: {line}")
        if result["existing"]:
            print(f"kept:    {result['existing']} existing (the operator's copy wins)")
    finally:
        store.close()
    return 0


def register_skill_commands(
    subcommands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    """``skep skill propose|list|show|test|approve|reject|export|import`` (v4/v31)."""
    skill = subcommands.add_parser("skill", help="learned-skill lifecycle (v4)")
    skill_sub = skill.add_subparsers(dest="skill_command")

    propose_p = skill_sub.add_parser(
        "propose", help="generalize successful runs into draft candidates"
    )
    propose_p.add_argument(
        "--min-occurrences",
        type=int,
        default=DEFAULT_MIN_OCCURRENCES,
        help=f"matching successful runs that make a pattern (default {DEFAULT_MIN_OCCURRENCES})",
    )
    propose_p.add_argument(
        "--max-params",
        type=int,
        default=DEFAULT_MAX_PARAMS,
        help=f"reject a generalization with more varying slots (default {DEFAULT_MAX_PARAMS})",
    )
    propose_p.set_defaults(func=cmd_skill_propose)

    list_p = skill_sub.add_parser("list", help="list skill candidates and their state")
    list_p.set_defaults(func=cmd_skill_list)

    show_p = skill_sub.add_parser("show", help="show one candidate in full")
    show_p.add_argument("name")
    show_p.set_defaults(func=cmd_skill_show)

    test_p = skill_sub.add_parser(
        "test", help="test a draft against a real repo (the G10 gate); pass -> 'tested'"
    )
    test_p.add_argument("name")
    test_p.add_argument("repo", type=Path)
    test_p.add_argument(
        "--param", action="append", default=[], metavar="KEY=VALUE", help="fill a parameter"
    )
    test_p.add_argument("--ref", default=None, help="git ref to base the test worktree on")
    test_p.add_argument(
        "--worker-cmd",
        default=None,
        help=(
            "worker argv prefix for a coding-caste candidate "
            "(default: $SKEP_WORKER_CMD or skep's minimal coding worker)"
        ),
    )
    test_p.set_defaults(func=cmd_skill_test)

    approve_p = skill_sub.add_parser(
        "approve", help="HUMAN gate: promote a tested candidate into the registry"
    )
    approve_p.add_argument("name")
    approve_p.add_argument(
        "--as",
        dest="as_name",
        default=None,
        metavar="NAME",
        help="register the skill under a friendlier name (default: the candidate name)",
    )
    approve_p.add_argument("--actor", default=None, help="who is approving (default: $USER)")
    approve_p.add_argument("--note", default=None)
    approve_p.set_defaults(func=cmd_skill_approve)

    reject_p = skill_sub.add_parser("reject", help="HUMAN gate: deny a candidate")
    reject_p.add_argument("name")
    reject_p.add_argument("--actor", default=None, help="who is rejecting (default: $USER)")
    reject_p.add_argument("--note", default=None)
    reject_p.set_defaults(func=cmd_skill_reject)

    # v31: portable, signed skill distribution.
    export_p = skill_sub.add_parser(
        "export", help="export a registry skill as a signed, portable bundle (v31)"
    )
    export_p.add_argument("name")
    export_p.add_argument("--out", default=None, metavar="FILE", help="write the bundle here")
    export_p.set_defaults(func=cmd_skill_export)

    # v83-F12: the shipped seed shelf (zero-grant; operator copies win).
    seed_p = skill_sub.add_parser(
        "seed",
        help="load the shipped seed skills into the registry (zero-grant only; "
        "existing names and operator deletes are honored)",
    )
    seed_p.set_defaults(func=cmd_skill_seed)

    # v85-F2: external Agent Skills shelves (~/.claude/skills convention) —
    # the seed rules, pointed outward.
    shelf_p = skill_sub.add_parser(
        "shelf",
        help="register external SKILL.md shelf directories (zero-grant, synced "
        "at serve start); no args lists them",
    )
    shelf_p.add_argument(
        "shelf_action", nargs="?", choices=("add", "remove"), help="add or remove a shelf"
    )
    shelf_p.add_argument("path", nargs="?", help="shelf directory, e.g. ~/.claude/skills")
    shelf_p.set_defaults(func=cmd_skill_shelf)

    # v85-F3: the pack ladder — packages walk draft -> trial -> active.
    promote_p = skill_sub.add_parser(
        "promote",
        help="HUMAN gate: walk a drafted skill pack through the v17 ladder "
        "(syntax trial, then activation with the typed grants)",
    )
    promote_p.add_argument("pack_id", help="pack id from `skep skill packs`")
    promote_p.add_argument(
        "--allow-script",
        action="append",
        metavar="COMMAND",
        help="grant ONE shell command to the activated skill (repeatable)",
    )
    promote_p.set_defaults(func=cmd_skill_promote)

    packs_p = skill_sub.add_parser(
        "packs", help="list script-shipping skill packs and their ladder state"
    )
    packs_p.set_defaults(func=cmd_skill_packs)

    # v44-F6: the SKILL.md on-ramp (Hermes pack migration) — same human gate.
    import_md_p = skill_sub.add_parser(
        "import-md",
        help="HUMAN gate: convert a SKILL.md pack dir, review grants, --approve to admit (v44)",
    )
    import_md_p.add_argument("directory", help="pack directory containing SKILL.md")
    import_md_p.add_argument(
        "--allow-script",
        action="append",
        default=[],
        metavar="COMMAND",
        help="grant ONE shell command to the skill (repeatable; shipped scripts grant nothing)",
    )
    import_md_p.add_argument(
        "--approve", action="store_true", help="admit the skill after reviewing its grants"
    )
    import_md_p.add_argument(
        "--as", dest="as_name", default=None, metavar="NAME", help="import under this name"
    )
    import_md_p.set_defaults(func=cmd_skill_import_md)

    import_p = skill_sub.add_parser(
        "import",
        help="HUMAN gate: review a signed skill bundle's grants, then --approve to admit it (v31)",
    )
    import_p.add_argument("file")
    import_p.add_argument(
        "--approve",
        action="store_true",
        help="admit the skill into the registry after reviewing its disclosed grants",
    )
    import_p.add_argument(
        "--as",
        dest="as_name",
        default=None,
        metavar="NAME",
        help="import under a different name (avoids a collision; no silent overwrite)",
    )
    import_p.set_defaults(func=cmd_skill_import)
