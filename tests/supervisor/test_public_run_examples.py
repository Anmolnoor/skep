from __future__ import annotations

import html
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

PUBLIC_DOCS = (
    ROOT / "README.md",
    ROOT / "docs" / "approvals.md",
    ROOT / "docs" / "cli-reference.md",
    ROOT / "docs" / "configuration.md",
    ROOT / "docs" / "demo-gif.md",
    ROOT / "docs" / "how-it-works.md",
    ROOT / "docs" / "how-to-use-on-new-machine.md",
    ROOT / "docs" / "index.html",
    ROOT / "docs" / "quickstart.md",
    ROOT / "docs" / "sandboxing.md",
    ROOT / "docs" / "workers.md",
    ROOT / "examples" / "skep-demo" / "README.md",
)

FENCED_BLOCK_RE = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)
INLINE_CODE_RE = re.compile(r"`([^`\n]*skep run[^`\n]*)`")
HTML_COMMAND_RE = re.compile(r"<(?:pre|li)\b[^>]*>(.*?)</(?:pre|li)>", re.DOTALL)
HTML_TAG_RE = re.compile(r"<[^>]+>")
SUBCOMMAND_HOME_RE = re.compile(
    r"\b(?:uv run )?skep\s+"
    r"(?:setup|doctor|status|start|run|review|schedule|tick|template|skill|serve)\b"
    r"[^\n`]*\s--home\b"
)


def _copy_paste_run_examples(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    snippets = FENCED_BLOCK_RE.findall(text)
    snippets.extend(INLINE_CODE_RE.findall(text))

    if path.suffix == ".html":
        for raw in HTML_COMMAND_RE.findall(text):
            snippets.append(html.unescape(HTML_TAG_RE.sub(" ", raw)))

    examples = []
    for snippet in snippets:
        normalized = " ".join(snippet.split())
        if "skep run" not in normalized:
            continue
        if normalized in {"skep run", "uv run skep run"}:
            continue
        if "->" in normalized or "skep run [repo]" in normalized:
            continue
        examples.append(normalized)
    return examples


def test_public_run_examples_choose_execution_mode() -> None:
    missing = []
    for path in PUBLIC_DOCS:
        for example in _copy_paste_run_examples(path):
            if "--execution-mode" not in example:
                missing.append(f"{path.relative_to(ROOT)}: {example}")

    assert not missing, "run examples must include --execution-mode:\n" + "\n".join(missing)


def test_public_docs_place_global_home_before_subcommand() -> None:
    bad_examples = []
    for path in PUBLIC_DOCS:
        text = path.read_text(encoding="utf-8")
        for match in SUBCOMMAND_HOME_RE.finditer(text):
            bad_examples.append(f"{path.relative_to(ROOT)}: {match.group(0).strip()}")

    assert not bad_examples, "--home is global; place it before the subcommand:\n" + "\n".join(
        bad_examples
    )


def test_source_dashboard_docs_use_same_home_for_cli_and_serve() -> None:
    quickstart = (ROOT / "docs" / "quickstart.md").read_text(encoding="utf-8")
    new_machine = (ROOT / "docs" / "how-to-use-on-new-machine.md").read_text(encoding="utf-8")

    assert "uv run skep serve --host 127.0.0.1 --port 8765" in quickstart
    assert "--home .skep-dev serve" not in quickstart
    assert 'export SKEP_HOME="$PWD/.skep-dev"' in new_machine
    assert "uv run skep serve --host 127.0.0.1 --port 8765" in new_machine
