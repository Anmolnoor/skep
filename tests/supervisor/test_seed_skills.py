"""v83-F12 (ADR 0043): the seed loader — zero-grant, operator-wins, durable
deletes. The F13 shelf content is pinned in its own tests; this file pins
the loader's rules against a fixture shelf."""

from __future__ import annotations

from pathlib import Path

import pytest

from skep.supervisor import SupervisorConfig
from skep.supervisor.seed_skills import (
    EXTERNAL_PROVENANCE,
    SEED_PROVENANCE,
    add_skill_shelf,
    load_seed_skills,
    remove_skill_shelf,
    seeds_root,
    skill_shelves,
    sync_skill_shelves,
)
from skep.supervisor.store import RunStore
from skep.supervisor.templates import WorkflowTemplate


def _write_seed(root: Path, name: str, *, body: str = "Do the thing.") -> Path:
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name} description\n---\n\n# {name}\n\n{body}\n"
    )
    return directory


def test_loader_is_idempotent_and_zero_grant(tmp_path: Path) -> None:
    shelf = tmp_path / "shelf"
    _write_seed(shelf, "research-a-topic")
    scripted = _write_seed(shelf, "sneaky")
    (scripted / "scripts").mkdir()
    (scripted / "scripts" / "run.sh").write_text("#!/bin/sh\n")

    store = RunStore(tmp_path / "s.sqlite3")
    try:
        first = load_seed_skills(store, root=shelf)
        assert first["loaded"] == ["research-a-topic"]
        assert any("zero-grant" in line for line in first["skipped"])
        loaded = store.get_template("research-a-topic")
        assert loaded is not None
        assert loaded.provenance == SEED_PROVENANCE
        assert loaded.shell_allowlist == ()  # nothing granted, ever
        assert store.get_template("sneaky") is None

        second = load_seed_skills(store, root=shelf)
        assert second["loaded"] == [] and second["existing"] == 1
    finally:
        store.close()


def test_operator_copies_win_and_deletes_are_durable(tmp_path: Path) -> None:
    shelf = tmp_path / "shelf"
    _write_seed(shelf, "review-a-pr")
    _write_seed(shelf, "daily-briefing")

    store = RunStore(tmp_path / "s.sqlite3")
    try:
        # The operator already has a template under a seed's name — it wins.
        store.add_template(
            WorkflowTemplate(
                name="review-a-pr",
                instructions="the operator's own recipe",
                description="mine",
                provenance="user",
            )
        )
        result = load_seed_skills(store, root=shelf)
        assert result["loaded"] == ["daily-briefing"]
        kept = store.get_template("review-a-pr")
        assert kept is not None and kept.instructions == "the operator's own recipe"

        # Deleting a seed tombstones it — the next sync never resurrects it.
        from skep.supervisor.serve.tools import execute_mutation

        execute_mutation(
            "delete_skill",
            {"name": "daily-briefing"},
            store=store,
            holder=None,  # type: ignore[arg-type]
            runner=None,  # type: ignore[arg-type]
            actor="tester",
        )
        again = load_seed_skills(store, root=shelf)
        assert again["loaded"] == []
        assert any("tombstone" in line for line in again["skipped"])
        assert store.get_template("daily-briefing") is None
    finally:
        store.close()


def test_operator_skills_outrank_the_shelf_in_the_index(tmp_path: Path) -> None:
    """review item 6: seeds must never evict operator skills from the prompt.
    v99-F2 removed the cap, so nothing evicts anything — the ordering now
    decides what the model reads FIRST, which is the surviving half of the
    concern."""
    from skep.supervisor.serve.chat import skill_index_block

    store = RunStore(tmp_path / "s.sqlite3")
    try:
        for index in range(20):
            store.add_template(
                WorkflowTemplate(
                    name=f"seed-{index:02d}",
                    instructions="stock",
                    description="stock seed",
                    provenance="seed",
                )
            )
        store.add_template(
            WorkflowTemplate(
                name="zz-operator-skill",
                instructions="mine",
                description="the operator's own",
                provenance="user",
            )
        )
        block = skill_index_block(store)
        # Alphabetically last, but provenance ranks it AHEAD of every seed
        # (v126-F8: on its own described line, above the seed names).
        assert block.splitlines()[1] == "- zz-operator-skill: the operator's own"
        listed = [name.strip() for name in block.splitlines()[-1].split(",")]
        assert len(listed) == 20  # every seed still listed, none evicted
    finally:
        store.close()


def test_the_real_shelf_location_is_in_package() -> None:
    root = seeds_root()
    assert root.parts[-3:] == ("skep", "seeds", "skills")


# ---------- v83-F13: the real shelf ----------


def _real_seed_dirs() -> list[Path]:
    root = seeds_root()
    return sorted(d for d in root.iterdir() if (d / "SKILL.md").is_file())


def test_the_shelf_is_stocked_and_every_seed_parses() -> None:
    """v83-F13: the library finally has books — and each one parses,
    carries a description, and grants nothing (ADR 0043)."""
    from skep.supervisor.skill_md import parse_skill_md

    dirs = _real_seed_dirs()
    assert len(dirs) >= 83  # v100: four more (F1-F4)
    for directory in dirs:
        pack = parse_skill_md(directory)
        assert pack.name == directory.name
        assert pack.description and pack.description != pack.name
        assert pack.scripts_found == ()  # zero-grant by construction
        assert "Tools:" in pack.instructions, directory.name


def test_every_seed_names_only_tools_that_exist() -> None:
    """The v25 lockstep lesson applied to content: a seed teaching a tool
    that does not exist trains the Queen to hallucinate — the shelf and
    the tool surface move together."""
    import re

    from skep.supervisor.serve.tools import TOOL_SPECS
    from skep.supervisor.skill_md import parse_skill_md

    real = {t["function"]["name"] for t in TOOL_SPECS}
    for directory in _real_seed_dirs():
        pack = parse_skill_md(directory)
        for line in pack.instructions.splitlines():
            if not line.startswith("Tools:"):
                continue
            named = {t.strip() for t in re.split(r"[,\s]+", line[len("Tools:") :]) if t.strip()}
            unknown = named - real
            assert not unknown, f"{pack.name} names unknown tools: {sorted(unknown)}"


# ---------- v84: the phase-2 shelf ----------


# v84-A1: verbs that mutate. None of these may ever appear inside a named
# shell grant in any seed — mutations run ungranted so every invocation
# cards, and the card's argv is the verbatim payload.
_GRANT_MUTATION_VERBS = frozenset(
    {"send", "upload", "delete", "sync", "post", "push", "move", "rm", "kick", "ban"}
)


def _seed_bodies() -> list[tuple[str, str]]:
    return [(d.name, (d / "SKILL.md").read_text(encoding="utf-8")) for d in _real_seed_dirs()]


def test_seed_grants_are_read_verb_prefixes_only() -> None:
    """v84-A1 (the review's headline): a seed may only name shell grants as
    multi-token prefixes scoped to read verbs — never a bare binary (one
    grant would silently cover the mutations the same seed claims card),
    never a mutating verb. curl/wget are never a write path in any seed:
    REST writes are MCP-server-or-nothing."""
    import re

    for name, body in _seed_bodies():
        flat = re.sub(r"\s+", " ", body)
        for match in re.finditer(r"`allow_shell_command ([^`]+)`", flat):
            tokens = match.group(1).strip().split()
            assert len(tokens) >= 2, (
                f"{name}: bare-binary grant {tokens} — a grant is a full "
                "read-verb command prefix, never a whole CLI"
            )
            verbs = {token.strip(".,;:'\"") for token in tokens}
            assert not (verbs & _GRANT_MUTATION_VERBS), (
                f"{name}: mutation verb inside a named grant {tokens} — "
                "mutations run ungranted so every invocation cards"
            )
        # A grant mention outside backticks must not smuggle a mutation verb
        # either ("ask for allow_shell_command himalaya message send").
        for verb in _GRANT_MUTATION_VERBS:
            assert not re.search(rf"allow_shell_command[ ,`]+\S+ {verb}\b", flat), (
                f"{name}: grant text names mutation verb {verb!r}"
            )
        # curl/wget never co-occur with a write method or payload flag.
        assert not re.search(
            r"\b(curl|wget)\b[^`]{0,80}(-X ?(POST|PUT|PATCH|DELETE)|--data\b|-d )",
            flat,
            re.IGNORECASE,
        ), f"{name}: curl/wget taught as a write path — writes are MCP-or-nothing"


def test_batch_card_seeds_pin_the_size_bound() -> None:
    """v84-A3: the email-cleanup batch card is bounded (≤50 per card, split
    into complete sequential cards, count == list, never elide) — silent
    truncation on a destructive batch is approving deletions unseen."""
    body = (seeds_root() / "email-cleanup" / "SKILL.md").read_text(encoding="utf-8")
    assert "50 messages per card" in body
    assert "batch 2 of 4" in body  # the split shape, taught concretely
    assert "count and the list must always agree" in body
    assert "Never summarize" in body and "never elide" in body


def test_outbound_seeds_pin_the_compose_then_card_recipe() -> None:
    """v84-F4 (ADR 0044): the social/mail seeds hard-code compose → verbatim
    card → confirm, and never request a shell grant for posting. The
    instruction stays load-bearing; the MECHANISM behind it (the
    never-grantable class) is pinned in test_shell_prefixes."""
    import re

    for name in ("xurl", "discord-web-api", "himalaya"):
        body = (seeds_root() / name / "SKILL.md").read_text(encoding="utf-8")
        flat = re.sub(r"\s+", " ", body)
        assert "card" in flat and "confirm" in flat.lower(), name
        assert "ever request a shell grant" in flat, name  # Never/never


def test_the_loaded_shelf_keeps_the_prompt_floor_bounded(tmp_path: Path) -> None:
    """review item 6: measured before landing, not discovered in August.
    v99-F2: names-only holds the whole shelf in LESS space than the capped
    block spent on 20 entries, so the bound survives the cap's removal."""
    from skep.supervisor.serve.chat import skill_index_block

    store = RunStore(tmp_path / "s.sqlite3")
    try:
        result = load_seed_skills(store)
        assert len(result["loaded"]) >= 83  # v100: four seeds added
        block = skill_index_block(store)
        assert len(block) < 2_400  # the fold-in, measured with the FULL shelf
        listed = block.splitlines()[-1].split(", ")
        assert len(listed) == len(result["loaded"])  # every seed, none hidden
    finally:
        store.close()


def test_mcp_backed_seeds_teach_the_setup_card_first() -> None:
    """v84-F7: the two MCP-backed creative seeds never touch the vendor
    surface without the registered server + grants — the setup card is the
    first step, in the seed's own text."""
    for name in ("comfyui", "touchdesigner-mcp"):
        body = (seeds_root() / name / "SKILL.md").read_text(encoding="utf-8")
        assert "register_mcp_server" in body, name
        assert "allow_mcp_tool" in body, name


def test_external_shelves_load_with_external_provenance(tmp_path: Path) -> None:
    """v85-F2: an operator-registered shelf syncs under the seed rules —
    zero-grant only, tombstones, operator wins — with provenance 'external'."""
    shelf = tmp_path / "claude-skills"
    _write_seed(shelf, "community-skill")
    scripted = _write_seed(shelf, "scripted-skill")
    (scripted / "scripts").mkdir()
    (scripted / "scripts" / "run.sh").write_text("#!/bin/sh\n")

    store = RunStore(tmp_path / "s.sqlite3")
    try:
        add_skill_shelf(store, shelf)
        assert skill_shelves(store) == [str(shelf)]
        add_skill_shelf(store, shelf)  # idempotent
        assert skill_shelves(store) == [str(shelf)]

        reports = sync_skill_shelves(store)
        assert reports[str(shelf)]["loaded"] == ["community-skill"]
        loaded = store.get_template("community-skill")
        assert loaded is not None
        assert loaded.provenance == EXTERNAL_PROVENANCE
        assert loaded.shell_allowlist == ()

        # v85-F6: the script pack drafts onto the ladder — no grants, no
        # registry entry, nothing runnable until promoted.
        from skep.supervisor.skill_packs import load_packs, suspend_pack

        assert reports[str(shelf)]["drafted"] == ["scripted-skill"]
        record = load_packs(store)["scripted-skill"]
        assert record.state == "draft" and record.grants == ()
        assert record.origin == f"shelf:{shelf}"
        assert store.get_template("scripted-skill") is None

        # Re-sync is a no-op: the record (any state) wins.
        again = sync_skill_shelves(store)[str(shelf)]
        assert again["drafted"] == [] and again["existing"] == 2

        # A rolled-back pack is never silently re-drafted.
        suspend_pack(store, "scripted-skill", rollback=True)
        after_rollback = sync_skill_shelves(store)[str(shelf)]
        assert after_rollback["drafted"] == []
        assert load_packs(store)["scripted-skill"].state == "rolled_back"

        remove_skill_shelf(store, shelf)
        assert skill_shelves(store) == []
        # Removal never reaches into the registry (deletes are individual).
        assert store.get_template("community-skill") is not None
        assert sync_skill_shelves(store) == {}
    finally:
        store.close()


def test_add_skill_shelf_refuses_a_missing_directory(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "s.sqlite3")
    try:
        with pytest.raises(ValueError, match="not a directory"):
            add_skill_shelf(store, tmp_path / "nope")
        assert skill_shelves(store) == []
    finally:
        store.close()


def test_skill_shelf_cli_round_trip(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    import argparse

    from skep.supervisor.skill_cmds import cmd_skill_shelf

    home = tmp_path / "home"
    shelf = tmp_path / "claude-skills"
    _write_seed(shelf, "community-skill")

    def _args(action: str | None, path: str | None) -> argparse.Namespace:
        return argparse.Namespace(home=home, shelf_action=action, path=path)

    assert cmd_skill_shelf(_args(None, None)) == 0
    assert "no external shelves" in capsys.readouterr().out
    assert cmd_skill_shelf(_args("add", str(shelf))) == 0
    out = capsys.readouterr().out
    assert "registered shelf" in out and "community-skill" in out
    assert cmd_skill_shelf(_args(None, None)) == 0
    assert str(shelf.resolve()) in capsys.readouterr().out
    assert cmd_skill_shelf(_args("remove", str(shelf))) == 0
    assert "removed shelf" in capsys.readouterr().out
    assert cmd_skill_shelf(_args("add", None)) != 0  # PATH required


def test_the_git_skills_cover_the_verb_this_round_added() -> None:
    """v103-F4/F5. These two seeds exist because guessing about git produced 13
    unmerged branches on one repo. Their `Tools:` lines are checked against the
    real surface by test_every_seed_names_only_tools_that_exist above; this pins
    that they actually reach the verb the round was about — a git skill written
    before merge_branch existed would still pass every other check."""
    from skep.supervisor.skill_md import parse_skill_md

    for name in ("git-and-github", "briefing-a-worker-about-git"):
        pack = parse_skill_md(seeds_root() / name)
        assert "merge_branch" in pack.instructions, name


def test_the_git_skills_state_the_deny_rather_than_softening_it() -> None:
    """I12: the boundary is the whole point of these two seeds. A skill that
    says "prefer not to" instead of "denied, and no grant overrides it" is how
    the Queen ends up burning a dispatch to discover the rule."""
    from skep.supervisor.skill_md import parse_skill_md

    queen = parse_skill_md(seeds_root() / "git-and-github").instructions
    worker = parse_skill_md(seeds_root() / "briefing-a-worker-about-git").instructions

    for body in (queen, worker):
        assert "no grant" in body.lower()
    # The Queen is bound by the same list as a worker (v83-F9) — the seed has
    # to say so, because "the worker can't but I can" is the exact wrong model.
    assert "binds you exactly" in queen
    # And the worker seed has to give the reason, not just the rule.
    assert "diff between the working tree and" in worker


# v126-F2: seed and shelf updates reach installs that never edited them; an
# operator edit still wins and is reported, never silently kept.


def _rewrite(root: Path, name: str, body: str) -> None:
    (root / name / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name} description\n---\n\n# {name}\n\n{body}\n"
    )


def _patch(store: RunStore, name: str, text: str) -> None:
    """What patch_skill leaves behind: same provenance, same created_at."""
    import dataclasses

    row = store.get_template(name)
    assert row is not None
    store.add_template(dataclasses.replace(row, instructions=row.instructions + text))


def _template_events(store: RunStore, action: str) -> list[str]:
    return [
        e.item_id for e in store.note_task_events() if e.kind == "template" and e.action == action
    ]


def test_an_unedited_seed_refreshes_and_an_edited_one_is_kept(tmp_path: Path) -> None:
    shelf = tmp_path / "shelf"
    _write_seed(shelf, "plain")
    _write_seed(shelf, "edited")
    store = RunStore(tmp_path / "s.sqlite3")
    try:
        load_seed_skills(store, root=shelf)
        _patch(store, "edited", "\nmy tweak")
        _rewrite(shelf, "plain", "Do the better thing.")
        _rewrite(shelf, "edited", "Do the better thing.")

        result = load_seed_skills(store, root=shelf)
        assert result["refreshed"] == ["plain"]
        assert result["diverged"] == ["edited"]
        plain = store.get_template("plain")
        assert plain is not None and "better thing" in plain.instructions
        assert plain.provenance == SEED_PROVENANCE
        edited = store.get_template("edited")
        assert edited is not None and "my tweak" in edited.instructions
        assert _template_events(store, "refreshed") == ["plain"]

        again = load_seed_skills(store, root=shelf)
        assert again["refreshed"] == [] and again["diverged"] == ["edited"]
        assert again["existing"] == 1
    finally:
        store.close()


def test_a_missing_shelf_reports_nothing(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "s.sqlite3")
    try:
        result = load_seed_skills(store, root=tmp_path / "gone")
        assert result["loaded"] == result["refreshed"] == result["diverged"] == []
    finally:
        store.close()


def test_a_legacy_row_refreshes_only_on_positive_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from skep.supervisor import seed_skills

    shelf = tmp_path / "shelf"
    _write_seed(shelf, "old", body="Version one.")
    _write_seed(shelf, "current", body="Unchanged.")
    store = RunStore(tmp_path / "s.sqlite3")
    try:
        load_seed_skills(store, root=shelf)
        store.set_setting(seed_skills.SHIPPED_HASHES_KEY, {})  # a pre-v126 install
        v1 = store.get_template("old")
        assert v1 is not None
        _rewrite(shelf, "old", "Version two.")

        # No record of what shipped, no evidence: kept and reported as legacy.
        result = load_seed_skills(store, root=shelf)
        assert result["diverged"] == ["old (legacy)"]
        assert result["refreshed"] == []
        # A legacy row equal to today's text is evidence: its hash is recorded.
        assert seed_skills.shipped_hashes(store).keys() == {"seed::current"}

        # Positive evidence: the row hashes to a text that once shipped.
        monkeypatch.setitem(
            seed_skills.LEGACY_SHIPPED, "old", frozenset({seed_skills.skill_fingerprint(v1)})
        )
        result = load_seed_skills(store, root=shelf)
        assert result["refreshed"] == ["old"]
        refreshed = store.get_template("old")
        assert refreshed is not None and "Version two." in refreshed.instructions
    finally:
        store.close()


def test_the_real_youtube_seed_refreshes_from_both_prior_shipped_texts(tmp_path: Path) -> None:
    """The legacy evidence is real: each text the seed shipped before v126,
    loaded into a pre-v126 store, is refreshed to today's shipped text."""
    from skep.supervisor import seed_skills

    name = "summarize-a-youtube-video"
    fixtures = Path(__file__).parent.parent / "fixtures" / "legacy_seeds"
    for version in ("v1.0.1", "v122"):
        shelf = tmp_path / version / "shelf"
        (shelf / name).mkdir(parents=True)
        (shelf / name / "SKILL.md").write_text(
            (fixtures / f"{name}.{version}.md").read_text(encoding="utf-8")
        )
        store = RunStore(tmp_path / version / "s.sqlite3")
        try:
            load_seed_skills(store, root=shelf)
            store.set_setting(seed_skills.SHIPPED_HASHES_KEY, {})
            result = load_seed_skills(store)
            assert name in result["refreshed"], version
            assert result["diverged"] == []
        finally:
            store.close()


def test_shelf_edits_sync_and_the_operator_copy_still_wins(tmp_path: Path) -> None:
    shelf = tmp_path / "claude-skills"
    _write_seed(shelf, "community-skill")
    _write_seed(shelf, "tuned-skill")
    store = RunStore(tmp_path / "s.sqlite3")
    try:
        add_skill_shelf(store, shelf)
        sync_skill_shelves(store)
        _patch(store, "tuned-skill", "\nmine")
        _rewrite(shelf, "community-skill", "New upstream text.")
        _rewrite(shelf, "tuned-skill", "New upstream text.")
        report = sync_skill_shelves(store)[str(shelf)]
        assert report["refreshed"] == ["community-skill"]
        assert report["diverged"] == ["tuned-skill"]
        synced = store.get_template("community-skill")
        assert synced is not None and synced.provenance == EXTERNAL_PROVENANCE
        assert "New upstream text." in synced.instructions
    finally:
        store.close()


def test_skill_seed_refresh_takes_the_shipped_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import argparse

    from skep.supervisor import seed_skills
    from skep.supervisor.cli_cmds import build_config
    from skep.supervisor.skill_cmds import cmd_skill_seed

    home = tmp_path / "home"
    shelf = tmp_path / "shelf"
    _write_seed(shelf, "one")
    _write_seed(shelf, "two")
    monkeypatch.setattr(seed_skills, "seeds_root", lambda: shelf)

    def _args(refresh: list[str] | None = None, refresh_all: bool = False) -> argparse.Namespace:
        return argparse.Namespace(home=home, refresh=refresh, refresh_all=refresh_all)

    assert cmd_skill_seed(_args()) == 0
    external = tmp_path / "claude-skills"
    _write_seed(external, "ext")
    store = RunStore(build_config(home, None).db_path)
    try:
        _patch(store, "one", "\nedit one")
        _patch(store, "two", "\nedit two")
        add_skill_shelf(store, external)
        sync_skill_shelves(store)
        _patch(store, "ext", "\nedit ext")
        hashes = seed_skills.shipped_hashes(store)
        del hashes["seed::two"]  # "two" predates v126: a legacy row
        store.set_setting(seed_skills.SHIPPED_HASHES_KEY, hashes)
    finally:
        store.close()
    _rewrite(shelf, "one", "Shipped fix.")
    _rewrite(shelf, "two", "Shipped fix.")
    _rewrite(external, "ext", "Shipped fix.")
    capsys.readouterr()

    assert cmd_skill_seed(_args()) == 0
    out = capsys.readouterr().out
    assert "diverged: one — you edited it" in out and "skep skill seed --refresh one" in out
    assert "diverged: two (legacy) — no record of what shipped" in out
    assert "skep skill seed --refresh two`" in out

    assert cmd_skill_seed(_args(refresh=["one"])) == 0
    assert "refreshed: one" in capsys.readouterr().out
    assert cmd_skill_seed(_args(refresh_all=True)) == 0
    out = capsys.readouterr().out
    assert "refreshed: two" in out
    assert f"shelf {external}:" in out and "refreshed: ext" in out
    store = RunStore(build_config(home, None).db_path)
    try:
        for name in ("one", "two", "ext"):
            row = store.get_template(name)
            assert row is not None and "Shipped fix." in row.instructions
            assert "edit" not in row.instructions
        assert [e.actor for e in store.note_task_events() if e.action == "refreshed"] == [
            "operator"
        ] * 3
    finally:
        store.close()


def test_two_shelves_holding_one_name_stop_flipping(tmp_path: Path) -> None:
    """v126-F2 (review): the hash was keyed by name only, so shelves a/foo
    and b/foo each overwrote the other's record and every sync "refreshed".
    Keyed per shelf, the shelf that loaded foo keeps it; the other never
    recorded the name, so it is reported and left alone."""
    a, b = tmp_path / "a", tmp_path / "b"
    _write_seed(a, "foo", body="From a.")
    _write_seed(b, "foo", body="From b.")
    store = RunStore(tmp_path / "s.sqlite3")
    try:
        add_skill_shelf(store, a)
        add_skill_shelf(store, b)
        for _ in range(3):
            reports = sync_skill_shelves(store)
            assert reports[str(a)]["refreshed"] == reports[str(b)]["refreshed"] == []
        assert reports[str(b)]["diverged"] == ["foo (legacy)"]
        row = store.get_template("foo")
        assert row is not None and "From a." in row.instructions
        # --refresh respects the same key: b never recorded foo, a did.
        reports = sync_skill_shelves(store, refresh=["foo"])
        assert reports[str(b)]["refreshed"] == []
        row = store.get_template("foo")
        assert row is not None and "From a." in row.instructions
    finally:
        store.close()


def test_skill_seed_refresh_says_when_nothing_happened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import argparse

    from skep.supervisor import seed_skills
    from skep.supervisor.skill_cmds import cmd_skill_seed

    shelf = tmp_path / "shelf"
    _write_seed(shelf, "one")
    monkeypatch.setattr(seed_skills, "seeds_root", lambda: shelf)
    args = argparse.Namespace(home=tmp_path / "home", refresh=["nope", "one"], refresh_all=False)
    assert cmd_skill_seed(args) == 0
    out = capsys.readouterr().out
    assert "nothing to refresh for nope" in out
    assert "nothing to refresh for one" in out  # loaded fresh, already the shipped text


def test_serve_start_logs_refreshed_and_diverged_skills(
    config: SupervisorConfig, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    from skep.supervisor import seed_skills

    from .conftest import serve_client

    shelf = config.home / "seed-shelf"
    _write_seed(shelf, "plain")
    _write_seed(shelf, "edited")
    monkeypatch.setattr(seed_skills, "seeds_root", lambda: shelf)
    store = RunStore(config.db_path)
    try:
        load_seed_skills(store)
        _patch(store, "edited", "\nmine")
    finally:
        store.close()
    _rewrite(shelf, "plain", "New.")
    _rewrite(shelf, "edited", "New.")
    with caplog.at_level(logging.INFO, logger="skep.serve"), serve_client(config):
        pass
    (line,) = [r.getMessage() for r in caplog.records if "refreshed" in r.getMessage()]
    assert "refreshed: plain" in line and "diverged: edited" in line


# v126-F9: the shelf is credited, and every recipe can run where it is bound.


def test_the_notice_credits_hermes_agent() -> None:
    root = seeds_root().parents[2].parent  # src/skep/seeds/skills -> repo root
    notice = (root / "NOTICE").read_text(encoding="utf-8")
    assert "Hermes Agent" in notice and "2025 Nous Research" in notice and "MIT" in notice
    license_text = (seeds_root().parent / "LICENSE-hermes-agent.txt").read_text(encoding="utf-8")
    assert "Copyright (c) 2025 Nous Research" in license_text


def test_a_chat_recipe_seed_is_refused_for_worker_binding(tmp_path: Path) -> None:
    from skep.supervisor.scheduler import make_template_schedule
    from skep.supervisor.templates import TemplateError, instantiate, is_chat_recipe

    store = RunStore(tmp_path / "s.sqlite3")
    try:
        load_seed_skills(store)
        recipe = store.get_template("notion")
        assert recipe is not None and is_chat_recipe(recipe)
        with pytest.raises(TemplateError, match="Queen chat recipe") as refused:
            instantiate(recipe, repo=str(tmp_path))
        # v126-F9 (review): a prompt tick refuses call_mcp_tool and
        # register_mcp_server, so the error names them and points at chat.
        message = str(refused.value)
        assert "call_mcp_tool" in message and "register_mcp_server" in message
        # Phase 09 R1 (S8): a prompt tick holds store reads only, and
        # list_mcp_tools starts a server — refused too, so named too.
        assert "list_mcp_tools" in message.split("refuses")[-1]
        assert "run it from chat (view_skill notion)" in message
        assert "caste 'prompt'" not in message
        with pytest.raises(TemplateError, match="Queen chat recipe"):
            make_template_schedule(
                name="nightly-notion",
                template=recipe,
                params={},
                repo=tmp_path,
                interval_seconds=3600,
            )
    finally:
        store.close()
    # The marker is the seed's own Tools: line, not the word "seed": an
    # operator template with the same text still runs on a worker.
    mine = WorkflowTemplate(name="mine", instructions="Tools: read_url\n\nDo it.")
    assert not is_chat_recipe(mine)
    assert instantiate(mine, repo=str(tmp_path)).instructions.startswith("Tools:")


def test_a_recipe_whose_tools_a_prompt_tick_offers_suggests_the_prompt_schedule(
    tmp_path: Path,
) -> None:
    """v126-F9 (review): the prompt schedule is suggested only when every
    tool the recipe lists is one a read-only unattended tick offers."""
    from skep.supervisor.templates import TemplateError, instantiate

    recipe = WorkflowTemplate(
        name="digest", instructions="Tools: list_notes, list_runs\n\nDo it.", provenance="seed"
    )
    with pytest.raises(TemplateError, match="Queen chat recipe") as refused:
        instantiate(recipe, repo=str(tmp_path))
    assert "caste 'prompt'" in str(refused.value)
    assert "refuses" not in str(refused.value)


def test_notion_and_google_workspace_are_mcp_only() -> None:
    for name in ("notion", "google-workspace"):
        body = (seeds_root() / name / "SKILL.md").read_text(encoding="utf-8")
        assert "MCP-only" in body, name
        # read_url sends no headers or auth: the seed must not lean on it.
        assert "allow_fetch_domain" not in body, name
        assert "Notion-Version" not in body and "header" not in body.lower().replace(
            "sends no headers", ""
        ), name
        tools_line = next(line for line in body.splitlines() if line.startswith("Tools:"))
        assert "read_url" not in tools_line, name
