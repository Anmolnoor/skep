"""v17 Step 5: the `researcher` caste worker.

Deep research is a *worker run*, never Queen-side browsing: the researcher reads
a question and a source allowlist, fetches ONLY allow-listed sources (defense in
depth: the worker refuses an off-list source even before the sandbox does), and
produces file artifacts — ``report.md``, ``report.html``, ``sources.json`` — with
evidence for every source.

The network fetcher is injected, so the report-building logic is hermetic and
unit-testable. In a real run the fetch is bounded by the D1 network allowlist,
enforced by the sandbox on both platforms (Seatbelt on macOS; bwrap+netshim on
Linux since v28); the report records any sources it could not reach.

Invoked like any other contract worker so the supervisor's spawn path is uniform:

    python -m skep.workers.researcher --headless --task-file task.json --out result.json
"""

from __future__ import annotations

import argparse
import html
import json
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from pydantic import ValidationError

from skep.supervisor.netproxy import domain_allowed
from skep.worker_contract import (
    CONTRACT_VERSION,
    SUPPORTED_CONTRACT_RANGE,
    Artifact,
    CodingWorkerResult,
    CodingWorkerTask,
    EventType,
    TaskState,
    Usage,
    Verification,
    VerificationOutcome,
    check_supported,
)

from .html_text import html_to_text
from .worker_runtime import (
    EventStream as _EventStream,
)
from .worker_runtime import (
    Heartbeat as _Heartbeat,
)
from .worker_runtime import (
    manifest_fingerprint,
)
from .worker_runtime import (
    sha256_file as _sha256_file,
)
from .worker_runtime import (
    write_result as _write_result,
)

# fetch(url) -> page text, or raises for an unreachable/denied source.
Fetcher = Callable[[str], str]


def source_host(url: str) -> str:
    return urlparse(url).hostname or ""


def source_allowed(url: str, allowlist: Sequence[str]) -> bool:
    host = source_host(url)
    return bool(host) and domain_allowed(host, tuple(allowlist))


@dataclass(frozen=True)
class SourceEvidence:
    url: str
    allowed: bool
    fetched: bool
    detail: str  # excerpt on success, refusal/error reason otherwise


@dataclass(frozen=True)
class ResearchResult:
    question: str
    report_md: str
    report_html: str
    sources_json: str
    evidence: tuple[SourceEvidence, ...] = field(default_factory=tuple)


def _excerpt(text: str, limit: int = 240) -> str:
    collapsed = " ".join(text.split())
    return collapsed[:limit]


# A report exists to answer the question, so it carries the fetched text, not
# just excerpts (field test 2026-07-14: excerpt-only reports answered nothing).
# ponytail: flat per-source cap; relevance-ranked extraction is the upgrade path.
_REPORT_CONTENT_LIMIT = 10_000

# v43-F1: one good default (dark, tabular). No per-operator theming knob —
# the note-based preference store can drive theming if a second preference
# ever appears.
_REPORT_CSS = (
    ":root{color-scheme:dark}"
    "body{margin:0 auto;padding:2rem;background:#101418;color:#d8dee6;"
    "font:15px/1.6 system-ui,sans-serif;max-width:60rem}"
    "h1{font-size:1.5rem;border-bottom:1px solid #2a323c;padding-bottom:.5rem}"
    "h2{font-size:1.15rem;margin-top:2rem;color:#e6ebf2}"
    "h3{font-size:1rem;color:#9fb4cc;word-break:break-all}"
    "table{width:100%;border-collapse:collapse;font-size:.9rem}"
    "th,td{text-align:left;padding:.45rem .6rem;border-bottom:1px solid #232b34;"
    "vertical-align:top;word-break:break-all}"
    "th{color:#8b98a8;font-weight:600}"
    ".ok{color:#7fd18a}.miss{color:#e0a35f}.refused{color:#e07f7f}"
    "pre{background:#151b22;border:1px solid #232b34;border-radius:8px;padding:1rem;"
    "white-space:pre-wrap;word-break:break-word}"
)


def _status_label(item: SourceEvidence) -> str:
    return "fetched" if item.fetched else ("refused" if not item.allowed else "unreachable")


def _status_class(item: SourceEvidence) -> str:
    return "ok" if item.fetched else ("refused" if not item.allowed else "miss")


def run_research(
    question: str,
    sources: Sequence[str],
    allowlist: Sequence[str],
    *,
    fetch: Fetcher,
) -> ResearchResult:
    """Fetch only allow-listed sources and build the report artifacts (pure,
    given an injected fetcher)."""
    evidence: list[SourceEvidence] = []
    contents: list[tuple[str, str]] = []  # (url, readable text) per fetched source
    for url in sources:
        if not source_allowed(url, allowlist):
            evidence.append(
                SourceEvidence(url, allowed=False, fetched=False, detail="source not on allowlist")
            )
            continue
        try:
            body = fetch(url)
        except Exception as exc:  # an unreachable/denied source is recorded, not fatal
            evidence.append(
                SourceEvidence(
                    url, allowed=True, fetched=False, detail=str(exc) or exc.__class__.__name__
                )
            )
            continue
        evidence.append(SourceEvidence(url, allowed=True, fetched=True, detail=_excerpt(body)))
        contents.append((url, body[:_REPORT_CONTENT_LIMIT]))

    md_lines = [f"# Research: {question}", "", "## Sources", ""]
    for item in evidence:
        status = "fetched" if item.fetched else ("refused" if not item.allowed else "unreachable")
        md_lines.append(f"- [{status}] {item.url}: {item.detail}")
    if contents:
        md_lines.append("")
        md_lines.append("## Content")
        for url, text in contents:
            md_lines.extend(["", f"### {url}", "", text])
    report_md = "\n".join(md_lines) + "\n"

    # v43-F1: a self-contained styled document, dark by default — the report
    # the operator asked for, first time, with no restyle coding run. Inline
    # CSS only (the UI renders this in a fully locked-down iframe, sandbox="";
    # scripts would never execute, so the styling must be style-only anyway).
    rows_html = "".join(
        f'<tr><td class="{_status_class(e)}">{_status_label(e)}</td>'
        f"<td>{html.escape(e.url)}</td><td>{html.escape(e.detail)}</td></tr>"
        for e in evidence
    )
    content_html = "".join(
        f"<h3>{html.escape(url)}</h3><pre>{html.escape(text)}</pre>" for url, text in contents
    )
    report_html = (
        '<!doctype html><html><head><meta charset="utf-8">'
        f"<title>Research: {html.escape(question)}</title>"
        f"<style>{_REPORT_CSS}</style></head><body>"
        f"<h1>Research: {html.escape(question)}</h1>"
        "<h2>Sources</h2><table><thead>"
        "<tr><th>status</th><th>source</th><th>evidence</th></tr></thead>"
        f"<tbody>{rows_html}</tbody></table>"
        + (f"<h2>Content</h2>{content_html}" if contents else "")
        + "</body></html>"
    )

    sources_json = json.dumps(
        [
            {"url": e.url, "allowed": e.allowed, "fetched": e.fetched, "detail": e.detail}
            for e in evidence
        ],
        indent=2,
    )
    return ResearchResult(
        question=question,
        report_md=report_md,
        report_html=report_html,
        sources_json=sources_json,
        evidence=tuple(evidence),
    )


# -- contract-worker entrypoint (the runnable `researcher` caste) --------------

WORKER_VERSION = "researcher-0.1.0"
WORKER_CASTE = "researcher"

EXIT_COMPLETED = 0
EXIT_INVOCATION_ERROR = 2
EXIT_FAILED = 3
EXIT_REJECTED = 5

REPORT_MD_PATH = ".artifacts/report.md"
REPORT_HTML_PATH = ".artifacts/report.html"
SOURCES_JSON_PATH = ".artifacts/sources.json"

_FETCH_TIMEOUT_SECONDS = 30.0
_FETCH_MAX_BYTES = 65536
# Emit a heartbeat at least this often while fetching so the supervisor's
# heartbeat-loss backstop (Q3) doesn't mistake a slow source for a hung worker.
_HEARTBEAT_SECONDS = 5.0


def parse_question(instructions: str) -> str:
    """The research question from the task instructions.

    The deep-research template writes ``Question: <text>`` on its own line; a
    free-form dispatch is its own question."""
    for line in instructions.splitlines():
        if line.strip().lower().startswith("question:"):
            return line.split(":", 1)[1].strip()
    return instructions.strip()


def parse_sources(instructions: str) -> list[str]:
    """Seed URLs from the template's ``Sources: <url> <url> ...`` line (v46-F1).

    Empty/absent means the dispatch predates seed URLs (or the Queen passed
    none) — the caller falls back to the allowlist homepages."""
    for line in instructions.splitlines():
        if line.strip().lower().startswith("sources:"):
            return [u for u in line.split(":", 1)[1].split() if "://" in u]
    return []


def sources_from_allowlist(allowlist: Sequence[str]) -> list[str]:
    # The fallback when no Sources line arrived: the root page of each
    # allow-listed host. v46-F1 threads the discovered article URLs through
    # instead; this keeps free-form dispatches and pre-v46 schedules working.
    return [f"https://{host}/" for host in allowlist if host and "*" not in host]


# Field test 2026-07-14: sites bot-block urllib's default `Python-urllib/3.x`
# agent (glama.ai and mcp.so both 403'd). A browser-like UA is honest here —
# the request is a page read on the operator's behalf — and the allowlist, not
# the UA string, is the security boundary.
_FETCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0 skep-researcher"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
}


def _urllib_fetch(url: str) -> str:
    request = urllib.request.Request(url, method="GET", headers=_FETCH_HEADERS)
    with urllib.request.urlopen(request, timeout=_FETCH_TIMEOUT_SECONDS) as response:
        body = response.read(_FETCH_MAX_BYTES)
    return html_to_text(body.decode("utf-8", errors="replace"))


def _task_start_payload(task: CodingWorkerTask) -> dict[str, object]:
    payload: dict[str, object] = {
        "worker_version": WORKER_VERSION,
        "manifest_fingerprint": manifest_fingerprint(WORKER_VERSION, WORKER_CASTE),
    }
    if task.project_context is not None:
        payload["project_context"] = task.project_context.model_dump(mode="json")
    if task.dispatch_decision is not None:
        payload["dispatch_decision"] = task.dispatch_decision.model_dump(mode="json")
    return payload


def run_researcher_task(
    task_path: Path, out_path: Path, *, fetch: Fetcher = _urllib_fetch
) -> int:
    try:
        raw = json.loads(task_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"researcher worker: cannot read task file {task_path}: {exc}", flush=True)
        return EXIT_INVOCATION_ERROR

    task_id = str(raw.get("task_id") or "")
    trace_id = str(raw.get("trace_id") or "")
    workspace_raw = str(raw.get("workspace") or "")
    if not task_id or not trace_id or not workspace_raw:
        print("researcher worker: task envelope missing task_id/trace_id/workspace", flush=True)
        return EXIT_INVOCATION_ERROR

    workspace = Path(workspace_raw).expanduser()
    if not workspace.is_dir():
        print(f"researcher worker: workspace {workspace} does not exist", flush=True)
        return EXIT_INVOCATION_ERROR

    stream = _EventStream(
        workspace / ".events" / f"{task_id}.ndjson", task_id=task_id, trace_id=trace_id
    )

    def reject(reason: str) -> int:
        stream.emit(EventType.TASK_REJECTED, {"reason": reason, "worker_version": WORKER_VERSION})
        stream.emit(
            EventType.TASK_TERMINAL,
            {"status": TaskState.REJECTED.value, "summary": reason, "reason": "rejected"},
        )
        result = CodingWorkerResult(
            contract_version=CONTRACT_VERSION,
            task_id=task_id,
            trace_id=trace_id,
            status=TaskState.REJECTED,
            summary=reason,
            changed_files=[],
            commands=[],
            verification=Verification(
                outcome=VerificationOutcome.NOT_ATTEMPTED, details="rejected before execution"
            ),
            artifacts=[
                Artifact(
                    kind="event_log",
                    path=str(stream.path.relative_to(workspace)),
                    sha256=_sha256_file(stream.path),
                )
            ],
        )
        _write_result(out_path, result)
        return EXIT_REJECTED

    skew = check_supported(str(raw.get("contract_version") or ""), SUPPORTED_CONTRACT_RANGE)
    if skew is not None:
        return reject(str(skew))
    try:
        task = CodingWorkerTask.model_validate(raw)
    except ValidationError as exc:
        return reject(f"task envelope failed validation: {exc}")
    if task.worker_kind != WORKER_CASTE:
        return reject(
            f"this is the {WORKER_CASTE!r} worker but the task requests worker_kind "
            f"{task.worker_kind!r}; dispatch it to the worker that implements that caste."
        )

    return _execute(task, workspace, stream, out_path, fetch)


def _execute(
    task: CodingWorkerTask,
    workspace: Path,
    stream: _EventStream,
    out_path: Path,
    fetch: Fetcher,
) -> int:
    stream.emit(EventType.TASK_START, _task_start_payload(task))

    question = parse_question(task.instructions)
    allowlist = list(task.permissions.network)
    sources = parse_sources(task.instructions) or sources_from_allowlist(allowlist)
    stream.emit(
        EventType.PLAN_CREATED,
        {"steps": [f"read {url}" for url in sources] or ["no allow-listed sources to read"]},
    )

    with _Heartbeat(stream, "fetching sources", interval_seconds=_HEARTBEAT_SECONDS):
        research = run_research(question, sources, allowlist, fetch=fetch)

    # The report artifacts live under .artifacts so they are excluded from any
    # patch — research changes nothing in the repo, so nothing lands.
    artifacts_dir = workspace / ".artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    (workspace / REPORT_MD_PATH).write_text(research.report_md, encoding="utf-8")
    (workspace / REPORT_HTML_PATH).write_text(research.report_html, encoding="utf-8")
    (workspace / SOURCES_JSON_PATH).write_text(research.sources_json, encoding="utf-8")

    # Honest verification: a report that read nothing is not completed research.
    fetched = sum(1 for item in research.evidence if item.fetched)
    outcome = VerificationOutcome.PASSED if fetched else VerificationOutcome.FAILED
    detail = f"fetched {fetched}/{len(research.evidence)} allow-listed source(s)"
    stream.emit(
        EventType.VERIFY_RESULT,
        {"outcome": outcome.value, "details": detail, "commands": []},
    )

    status = TaskState.COMPLETED if fetched else TaskState.FAILED
    summary = (
        f"researched {question!r}: {detail}; wrote report.md, report.html, sources.json."
        if fetched
        else f"research did not complete: {detail}."
    )

    artifacts = [
        Artifact(kind="event_log", path=str(stream.path.relative_to(workspace)), sha256=""),
        *(
            Artifact(kind="file", path=path, sha256=_sha256_file(workspace / path))
            for path in (REPORT_MD_PATH, REPORT_HTML_PATH, SOURCES_JSON_PATH)
        ),
    ]
    stream.emit(EventType.TASK_TERMINAL, {"status": status.value, "summary": summary})
    # Now the event log is final; compute its hash for the artifact record.
    artifacts[0] = Artifact(
        kind="event_log",
        path=str(stream.path.relative_to(workspace)),
        sha256=_sha256_file(stream.path),
    )

    result = CodingWorkerResult(
        contract_version=CONTRACT_VERSION,
        task_id=task.task_id,
        trace_id=task.trace_id,
        status=status,
        summary=summary,
        changed_files=[],
        commands=[],
        verification=Verification(outcome=outcome, details=detail),
        artifacts=artifacts,
        usage=Usage(provider_calls=0, input_tokens=0, output_tokens=0),
    )
    _write_result(out_path, result)
    return EXIT_COMPLETED if status is TaskState.COMPLETED else EXIT_FAILED


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="skep-researcher-worker", description=__doc__)
    parser.add_argument("--headless", action="store_true", help="run one contract task and exit")
    parser.add_argument("--task-file", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    return run_researcher_task(args.task_file, args.out)


if __name__ == "__main__":
    raise SystemExit(main())
