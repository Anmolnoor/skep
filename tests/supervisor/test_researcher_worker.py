"""v17 Step 5: the researcher worker (injected fetcher, hermetic)."""

from __future__ import annotations

import json
from pathlib import Path

from skep.supervisor.contracts_io import mint_task, write_task_file
from skep.worker_contract import Permissions
from skep.workers.researcher import (
    REPORT_HTML_PATH,
    REPORT_MD_PATH,
    SOURCES_JSON_PATH,
    parse_question,
    parse_sources,
    run_research,
    run_researcher_task,
    source_allowed,
    sources_from_allowlist,
)


def test_source_allowlist_gate() -> None:
    allowlist = ["docs.python.org", "pypi.org"]
    assert source_allowed("https://docs.python.org/3/library/asyncio.html", allowlist) is True
    assert source_allowed("https://evil.example/leak", allowlist) is False
    assert source_allowed("not-a-url", allowlist) is False


def test_run_research_fetches_only_allowlisted_sources() -> None:
    fetched: list[str] = []

    def fetch(url: str) -> str:
        fetched.append(url)
        return f"contents of {url}"

    result = run_research(
        "how does asyncio work",
        sources=[
            "https://docs.python.org/3/asyncio.html",
            "https://evil.example/leak",  # not on allowlist -> never fetched
        ],
        allowlist=["docs.python.org"],
        fetch=fetch,
    )
    # The off-allowlist source is refused in the worker, never fetched.
    assert fetched == ["https://docs.python.org/3/asyncio.html"]
    refused = next(e for e in result.evidence if "evil" in e.url)
    assert refused.allowed is False and refused.fetched is False


def test_run_research_produces_three_artifacts_with_evidence() -> None:
    result = run_research(
        "q",
        sources=["https://pypi.org/project/httpx/"],
        allowlist=["pypi.org"],
        fetch=lambda _url: "httpx is an HTTP client for Python",
    )
    assert "# Research: q" in result.report_md
    assert "httpx is an HTTP client" in result.report_md
    assert "<h1>Research: q</h1>" in result.report_html
    sources = json.loads(result.sources_json)
    assert sources[0]["url"] == "https://pypi.org/project/httpx/"
    assert sources[0]["fetched"] is True


def test_report_carries_fetched_content_beyond_the_excerpt() -> None:
    # The evidence line is a ~240-char excerpt; the report must carry enough of
    # the page to actually answer the question (field test 2026-07-14).
    page = "the answer lives here " * 40  # ~880 chars, past the excerpt cap
    result = run_research(
        "q",
        sources=["https://pypi.org/x"],
        allowlist=["pypi.org"],
        fetch=lambda _url: page,
    )
    assert "## Content" in result.report_md
    assert page.strip() in result.report_md
    assert "<h2>Content</h2>" in result.report_html
    # An unfetchable run has no content section at all.
    empty = run_research("q", sources=[], allowlist=[], fetch=lambda _u: "")
    assert "## Content" not in empty.report_md


def test_unreachable_allowlisted_source_is_recorded_not_fatal() -> None:
    def boom(_url: str) -> str:
        raise ConnectionError("blocked by sandbox (deny-all on Linux)")

    result = run_research(
        "q",
        sources=["https://pypi.org/x"],
        allowlist=["pypi.org"],
        fetch=boom,
    )
    item = result.evidence[0]
    assert item.allowed is True and item.fetched is False
    assert "blocked by sandbox" in item.detail
    # The report still renders — an unreachable source does not crash the run.
    assert "unreachable" in result.report_md


def test_html_report_escapes_content() -> None:
    result = run_research(
        "<script>alert(1)</script>",
        sources=[],
        allowlist=[],
        fetch=lambda _u: "",
    )
    assert "<script>alert(1)</script>" not in result.report_html
    assert "&lt;script&gt;" in result.report_html


def test_html_report_is_styled_dark_with_a_sources_table() -> None:
    """v43-F1: the report the operator asked for, first time — a
    self-contained dark document with a real sources table, so 'make it
    pretty' coding runs stop existing."""
    result = run_research(
        "q",
        sources=["https://pypi.org/x", "https://evil.example/y"],
        allowlist=["pypi.org"],
        fetch=lambda _url: "answer " * 50,
    )
    page = result.report_html
    assert page.startswith("<!doctype html>")
    assert "<style>" in page and "color-scheme:dark" in page and "#101418" in page
    assert "<table>" in page and "<th>status</th>" in page
    assert '<td class="ok">fetched</td>' in page
    assert '<td class="refused">refused</td>' in page
    # Self-contained: no external assets (the UI renders this in sandbox="").
    assert "<link" not in page and "<script" not in page and 'src="' not in page
    # The markdown report keeps its stable shape (no styling leakage).
    assert result.report_md.startswith("# Research: q")


# -- the contract-worker entrypoint (the runnable caste) ----------------------


def test_completed_research_delivers_reports_to_the_workspace(tmp_path: Path) -> None:
    """v43-F2: reports land where the operator lives — identical bytes under
    ~/.skep/workspace/<slug>/, recorded as a workspace_delivery artifact; the
    audit copy stays the durable source of truth."""
    from skep.supervisor import RunStore
    from skep.supervisor.contracts_io import DEFAULT_BUDGET, mint_task
    from skep.supervisor.ingest import deliver_research_artifacts, research_delivery_slug

    store = RunStore(tmp_path / "s.sqlite3")
    try:
        task = mint_task(
            workspace=tmp_path / "ws",
            instructions="Research this.\nQuestion: Discord MCP servers?\nDepth: standard",
            budget=DEFAULT_BUDGET,
            worker_kind="researcher",
        )
        audit_task_dir = tmp_path / "audit" / task.task_id
        audit_task_dir.mkdir(parents=True)
        (audit_task_dir / "report.md").write_text("# Research\n")
        (audit_task_dir / "report.html").write_text("<!doctype html>x")
        (audit_task_dir / "sources.json").write_text("[]")
        delivery_root = tmp_path / "workspace"

        target = deliver_research_artifacts(
            store, task=task, audit_task_dir=audit_task_dir, delivery_root=delivery_root
        )
        assert target is not None and target.parent == delivery_root
        assert target.name.startswith("discord-mcp-servers-")  # slug + collision tail
        for name in ("report.md", "report.html", "sources.json"):
            assert (target / name).read_bytes() == (audit_task_dir / name).read_bytes()
        kinds = {kind: path for kind, path, _ in store.artifacts_for(task.task_id)}
        assert kinds["workspace_delivery"] == str(target)

        # Same question, different run → different directory (never collides).
        other = mint_task(
            workspace=tmp_path / "ws2",
            instructions="Question: Discord MCP servers?",
            budget=DEFAULT_BUDGET,
            worker_kind="researcher",
        )
        assert research_delivery_slug("Discord MCP servers?", task.task_id) != (
            research_delivery_slug("Discord MCP servers?", other.task_id)
        )

        # Non-researcher runs and empty audit dirs deliver nothing.
        coding = mint_task(
            workspace=tmp_path / "ws3", instructions="fix", budget=DEFAULT_BUDGET
        )
        assert (
            deliver_research_artifacts(
                store, task=coding, audit_task_dir=audit_task_dir, delivery_root=delivery_root
            )
            is None
        )
        empty_dir = tmp_path / "audit" / "empty"
        empty_dir.mkdir()
        assert (
            deliver_research_artifacts(
                store, task=task, audit_task_dir=empty_dir, delivery_root=delivery_root
            )
            is None
        )
    finally:
        store.close()


def test_parse_question_reads_the_template_line() -> None:
    instructions = (
        "Research this question and write a cited report.\n"
        "Question: why is the sky blue\nDepth: standard"
    )
    assert parse_question(instructions) == "why is the sky blue"
    # A free-form dispatch is its own question.
    assert parse_question("  how does uv lock work  ") == "how does uv lock work"


def test_sources_from_allowlist_skips_wildcards() -> None:
    assert sources_from_allowlist(["docs.python.org", "*", "*.example.com", ""]) == [
        "https://docs.python.org/"
    ]


def test_parse_sources_reads_the_template_line() -> None:
    instructions = (
        "Research this.\nQuestion: q\n"
        "Sources: https://a.com/article https://b.com/post not-a-url\n"
        "Sources are the listed URLs; each is fetched as readable text."
    )
    # URLs only — stray tokens and the prose sentence never become fetches.
    assert parse_sources(instructions) == ["https://a.com/article", "https://b.com/post"]
    # Absent or empty line → [] (caller falls back to allowlist homepages).
    assert parse_sources("Research this.\nQuestion: q") == []
    assert parse_sources("Question: q\nSources: \nprose") == []


def test_researcher_task_reads_seed_urls_and_refuses_offlist_seeds(tmp_path: Path) -> None:
    """v46-F1: seed URLs are fetched instead of homepages; the allowlist still
    refuses any seed whose host the operator never approved."""
    workspace, _task, task_file = _research_task_file(
        tmp_path,
        instructions=(
            "Research this.\nQuestion: how does asyncio work\n"
            "Sources: https://docs.python.org/3/library/asyncio-task.html "
            "https://evil.example/exfil"
        ),
    )
    out = tmp_path / "result.json"
    assert run_researcher_task(task_file, out, fetch=lambda url: f"contents of {url}") == 0
    sources = {
        s["url"]: s for s in json.loads((workspace / SOURCES_JSON_PATH).read_text())
    }
    assert sources["https://docs.python.org/3/library/asyncio-task.html"]["fetched"] is True
    refused = sources["https://evil.example/exfil"]
    assert refused["allowed"] is False and refused["fetched"] is False


def _research_task_file(
    tmp_path: Path,
    *,
    worker_kind: str = "researcher",
    instructions: str = "Research this.\nQuestion: how does asyncio work\nDepth: standard",
) -> tuple[Path, object, Path]:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    task = mint_task(
        workspace=workspace,
        instructions=instructions,
        worker_kind=worker_kind,
        permissions=Permissions(
            read=["workspace"],
            write=["workspace"],
            network=["docs.python.org"],
            env_allowlist=[],
        ),
    )
    return workspace, task, write_task_file(task, tmp_path / "task.json")


def test_researcher_task_writes_report_artifacts(tmp_path: Path) -> None:
    workspace, _task, task_file = _research_task_file(tmp_path)
    out = tmp_path / "result.json"

    exit_code = run_researcher_task(
        task_file, out, fetch=lambda url: f"contents of {url}"
    )

    assert exit_code == 0
    result = json.loads(out.read_text())
    assert result["status"] == "completed"
    assert result["changed_files"] == []  # research never edits the repo
    paths = {a["path"] for a in result["artifacts"] if a["kind"] == "file"}
    assert paths == {REPORT_MD_PATH, REPORT_HTML_PATH, SOURCES_JSON_PATH}
    report = (workspace / REPORT_MD_PATH).read_text()
    assert "# Research: how does asyncio work" in report
    sources = json.loads((workspace / SOURCES_JSON_PATH).read_text())
    assert sources[0]["url"] == "https://docs.python.org/"
    assert sources[0]["fetched"] is True


def test_researcher_task_fails_honestly_when_nothing_fetched(tmp_path: Path) -> None:
    def unreachable(_url: str) -> str:
        raise ConnectionError("blocked")

    _workspace, _task, task_file = _research_task_file(tmp_path)
    out = tmp_path / "result.json"
    assert run_researcher_task(task_file, out, fetch=unreachable) == 3
    result = json.loads(out.read_text())
    assert result["status"] == "failed"
    assert result["verification"]["outcome"] == "failed"


def test_researcher_rejects_wrong_caste(tmp_path: Path) -> None:
    _workspace, _task, task_file = _research_task_file(tmp_path, worker_kind="coding")
    out = tmp_path / "result.json"
    assert run_researcher_task(task_file, out, fetch=lambda _u: "") == 5
    assert json.loads(out.read_text())["status"] == "rejected"


def test_researcher_caste_is_registered_in_the_default_config(tmp_path: Path) -> None:
    # The v41 field-test failure: start_research dispatched caste `researcher`,
    # but no worker command was registered, so the run fell back to the coding
    # worker and was rejected. The registration is the fix — pin it.
    from skep.supervisor.cli_cmds import build_config

    config = build_config(tmp_path, None)
    command = config.command_for("researcher")
    assert command != config.worker_command
    assert command[-2:] == ("-m", "skep.workers.researcher")
