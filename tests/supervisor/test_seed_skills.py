"""v83-F12 (ADR 0043): the seed loader — zero-grant, operator-wins, durable
deletes. The F13 shelf content is pinned in its own tests; this file pins
the loader's rules against a fixture shelf."""

from __future__ import annotations

from pathlib import Path

import pytest

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
        # Alphabetically last, but provenance ranks it AHEAD of every seed.
        listed = [name.strip() for name in block.splitlines()[-1].split(",")]
        assert listed[0] == "zz-operator-skill"
        assert len(listed) == 21  # every seed still listed, none evicted
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
