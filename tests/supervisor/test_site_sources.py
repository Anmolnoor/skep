"""docs-site: the pins on the Astro site read its SOURCES, never its build.

``site/`` is the one place in the repository with npm. pytest must pass on a
machine where ``npm ci`` never ran, so nothing here reads ``site/dist`` or
``site/node_modules``: every check scans ``site/src``. The checks pass on the
foundation's stub pages and keep guarding once the real pages land.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from tests.supervisor.test_public_run_examples import SUBCOMMAND_HOME_RE

ROOT = Path(__file__).resolve().parents[2]
SITE = ROOT / "site"
SRC = SITE / "src"
PAGES = SRC / "pages"
SOURCE_SUFFIXES = {".astro", ".tsx", ".jsx", ".ts", ".mjs", ".js", ".css"}

# The one placeholder line a foundation stub carries. A page that still has it
# is a stub: the pins that need real content (adapter names) wait for the page.
STUB_LINE = "This page is being written."

# Plan §5.1: phrasings the code does not support. Exact strings, any case.
BANNED_PHRASINGS = (
    "never pushes",
    "nothing lands without",
    "tamper-proof",
    "tamper proof",
    "fully sandboxed",
    "planned adapters",
    "100% local",
    "default-deny",
    "only path to a commit",
    "group policy for agents",
    "github.com/Anmolnoor/skep/discussions",
)

# Plan §3.4 plus the old site's file names: each must stay a page so no old URL 404s.
REQUIRED_PAGES = (
    "agents",
    "blog",
    "changelog",
    "cli",
    "code-of-conduct",
    "doc",
    "docs",
    "first-run",
    "guides",
    "how-it-works",
    "index",
    "install",
    "open-source",
    "privacy",
    "roadmap",
    "security",
    "why-skep",
)


def _sources(*, with_tests: bool = True) -> list[Path]:
    files = sorted(
        path for path in SRC.rglob("*") if path.is_file() and path.suffix in SOURCE_SUFFIXES
    )
    if not with_tests:
        files = [path for path in files if not path.name.endswith(".test.mjs")]
    assert files, "site/src has no sources: the site pins would pass on nothing"
    return files


def _rel(path: Path) -> str:
    return str(path.relative_to(ROOT))


def _is_stub(path: Path) -> bool:
    return STUB_LINE in path.read_text(encoding="utf-8")


def _run_examples(text: str) -> list[str]:
    """Copy-paste ``skep run`` examples: the command followed by an argument
    (a quoted task, a path or a flag), with shell line continuations joined."""
    joined = re.sub(r"\\\s*\n\s*", " ", text)
    examples = []
    for line in joined.splitlines():
        for match in re.finditer(r"\bskep run\s+(?=[\"'~./$-]|&quot;)[^`<]*", line):
            examples.append(" ".join(match.group(0).split()))
    return examples


def test_run_example_scan_sees_examples_and_skips_prose() -> None:
    text = (
        "prose says skep run starts a task\n"
        "$ skep run ~/code/app \\\n    --task 'fix it'\n"
        '<code>skep run "fix the test" --execution-mode sandbox</code>\n'
    )
    assert _run_examples(text) == [
        "skep run ~/code/app --task 'fix it'",
        'skep run "fix the test" --execution-mode sandbox',
    ]


def test_site_run_examples_choose_execution_mode() -> None:
    missing = [
        f"{_rel(path)}: {example}"
        for path in _sources(with_tests=False)
        for example in _run_examples(path.read_text(encoding="utf-8"))
        if "--execution-mode" not in example
    ]
    assert not missing, "run examples must include --execution-mode:\n" + "\n".join(missing)


def test_site_places_global_home_before_the_subcommand() -> None:
    bad = [
        f"{_rel(path)}: {match.group(0).strip()}"
        for path in _sources(with_tests=False)
        for match in SUBCOMMAND_HOME_RE.finditer(path.read_text(encoding="utf-8"))
    ]
    assert not bad, "--home is global; place it before the subcommand:\n" + "\n".join(bad)


def test_site_types_no_version_or_contract_number() -> None:
    """Version and contract strings are read at build from pyproject.toml and
    worker_contract/version.py (site/src/lib/repo.mjs). A typed one goes stale
    at the next release, which is how the old site sat at v1.0.1 for months."""
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    version = pyproject["project"]["version"]
    contract_source = (ROOT / "src/skep/worker_contract/version.py").read_text(encoding="utf-8")
    contract_match = re.search(r'^CONTRACT_VERSION = "([^"]+)"', contract_source, re.MULTILINE)
    assert contract_match
    typed = re.compile(
        r"\bv\d+\.\d+\.\d+\b"
        rf"|(?<![\d.]){re.escape(version)}(?![\d.])"
        rf"|(?<![\d.]){re.escape(contract_match.group(1))}(?![\d.])"
        r"|worker contract \d"
    )
    found = [
        f"{_rel(path)}:{number}: {line.strip()}"
        for path in _sources(with_tests=False)
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if typed.search(line)
    ]
    assert not found, "typed version or contract number in site sources:\n" + "\n".join(found)


def test_site_reads_version_and_contract_from_the_python_sources() -> None:
    repo = (SRC / "lib" / "repo.mjs").read_text(encoding="utf-8")
    assert "'pyproject.toml'" in repo
    assert "'src/skep/worker_contract/version.py'" in repo
    for component in ("Header.astro", "Footer.astro"):
        assert "siteFacts()" in (SRC / "components" / component).read_text(encoding="utf-8")


def test_site_keeps_live_stats_client_side_with_a_build_fallback() -> None:
    script = (SRC / "scripts" / "site.js").read_text(encoding="utf-8")
    assert "api.github.com/repos/" in script
    assert "stargazers_count" in script
    assert "pypi.org/pypi/skep/json" in script
    header = (SRC / "components" / "Header.astro").read_text(encoding="utf-8")
    assert "<span data-version>v{version}</span>" in header


def test_banned_phrasings_are_absent() -> None:
    found = [
        f"{_rel(path)}: {phrase}"
        for path in _sources()
        for phrase in BANNED_PHRASINGS
        if phrase.lower() in path.read_text(encoding="utf-8").lower()
    ]
    assert not found, "phrasings the code does not support (plan §5.1):\n" + "\n".join(found)


def test_site_links_issues_not_discussions() -> None:
    """GitHub Discussions is disabled on the repository: every such link is dead."""
    discussions = re.compile(r"github\.com/[\w.-]+/[\w.-]+/discussions", re.IGNORECASE)
    found = [
        _rel(path) for path in _sources() if discussions.search(path.read_text(encoding="utf-8"))
    ]
    assert not found, f"links to GitHub Discussions: {found}"
    nav = (SRC / "lib" / "site-nav.mjs").read_text(encoding="utf-8")
    assert "ISSUES_URL = `${GH_URL}/issues`" in nav


def test_adapters_are_named_truthfully() -> None:
    """v33 shipped Codex and Aider; once the home and agents pages are written
    they name the shipped adapters ("planned adapters" is banned above)."""
    for name in ("index", "agents"):
        page = PAGES / f"{name}.astro"
        if _is_stub(page):
            continue
        text = page.read_text(encoding="utf-8")
        for adapter in ("Claude Code", "Codex", "Aider"):
            assert adapter in text, f"{_rel(page)} does not name {adapter}"


def test_every_page_uses_the_base_layout() -> None:
    pages = sorted(PAGES.rglob("*.astro"))
    assert pages
    for page in pages:
        text = page.read_text(encoding="utf-8")
        uses_base = "layouts/Base.astro'" in text and "<Base" in text
        uses_doc = "layouts/Doc.astro'" in text and "<Doc" in text
        assert uses_base or uses_doc, f"{_rel(page)} does not use the Base (or Doc) layout"


def test_every_old_url_and_planned_page_has_a_source() -> None:
    for name in REQUIRED_PAGES:
        assert (PAGES / f"{name}.astro").is_file(), f"site/src/pages/{name}.astro is missing"
    # While the hand-written site is still in the tree, its file list is the authority.
    for old in sorted((ROOT / "docs").glob("*.html")):
        assert (PAGES / f"{old.stem}.astro").is_file(), f"old URL /{old.name} has no page"


def test_nav_and_footer_reach_every_page() -> None:
    nav = (SRC / "lib" / "site-nav.mjs").read_text(encoding="utf-8")
    header = (SRC / "components" / "Header.astro").read_text(encoding="utf-8")
    unlisted = {"index", "doc", "kit"}  # home is the brand link; shim and kit are not navigation
    for page in sorted(PAGES.glob("*.astro")):
        if page.stem in unlisted:
            continue
        href = f"'/{page.stem}.html'"
        assert href in nav or href.replace("'", '"') in header, (
            f"/{page.stem}.html is in neither the header nav nor the footer"
        )
    assert 'href="/"' in header


def test_kit_page_is_noindex_and_out_of_the_nav() -> None:
    kit = (PAGES / "kit.astro").read_text(encoding="utf-8")
    assert "noindex" in kit
    assert "kit.html" not in (SRC / "lib" / "site-nav.mjs").read_text(encoding="utf-8")
    base = (SRC / "layouts" / "Base.astro").read_text(encoding="utf-8")
    assert '<meta name="robots" content="noindex" />' in base
    # noindex pages are also left out of site search.
    assert "data-pagefind-body={noindex ? undefined : ''}" in base


def test_old_doc_slug_map_matches_the_old_site() -> None:
    """The redirect shim's table is the old site's DOC_MAP, entry for entry."""
    slugs = (SRC / "lib" / "slugs.mjs").read_text(encoding="utf-8")
    table = slugs.split("export const OLD_DOC_SLUGS = {", 1)[1].split("};", 1)[0]
    new = dict(re.findall(r"^\s*'?([\w-]+)'?: '([\w-]+\.md)',$", table, re.MULTILINE))
    assert len(new) == 15
    for file in new.values():
        assert (ROOT / "docs" / file).is_file(), f"old slug points at missing docs/{file}"

    old_page = ROOT / "docs" / "doc.html"
    if old_page.is_file():  # deleted by the cutover PR; the table above then stands alone
        old = dict(
            re.findall(
                r"^\s*'([\w-]+)':\s*\{ file: '([\w-]+\.md)'",
                old_page.read_text(encoding="utf-8"),
                re.MULTILINE,
            )
        )
        assert new == old


def test_contrast_tokens_are_not_used_for_text() -> None:
    """Plan §3.3: --text-void (3.69:1) and --text-ghost (4.16-4.50:1) are below
    AA for text. They may colour a border or a rule, never `color:`."""
    as_text = re.compile(r"(?<![\w-])color:\s*var\(--text-(?:void|ghost)\)")
    found = [
        f"{_rel(path)}:{number}"
        for path in _sources()
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if as_text.search(line)
    ]
    assert not found, "low-contrast token used as a text colour:\n" + "\n".join(found)


def test_motion_is_gated_on_js_and_reduced_motion() -> None:
    """M3: server HTML is the final state; animation applies only under html.js
    and outside prefers-reduced-motion."""
    base_css = (SRC / "styles" / "base.css").read_text(encoding="utf-8")
    assert "@media (prefers-reduced-motion: no-preference)" in base_css
    assert "@media (prefers-reduced-motion: reduce)" in base_css
    assert "html.js .reveal" in base_css
    assert "@view-transition" in base_css
    layout = (SRC / "layouts" / "Base.astro").read_text(encoding="utf-8")
    assert "document.documentElement.classList.add('js')" in layout
    # No client router and no font CDN: page transitions are the CSS rule alone.
    for path in _sources():
        text = path.read_text(encoding="utf-8")
        assert "<ClientRouter" not in text, _rel(path)
        assert "fonts.googleapis.com" not in text, _rel(path)


def test_react_bits_copies_carry_their_licence() -> None:
    """The copies are MIT + Commons Clause: usable in the website, not
    redistributable as components. Each file says so; NOTICE says so; the
    sdist and the container image leave site/ out."""
    reactbits = SRC / "components" / "reactbits"
    licence = (reactbits / "LICENSE.md").read_text(encoding="utf-8")
    assert licence.startswith("MIT + Commons Clause License Condition v1.0")
    header = (
        "From React Bits (DavidHDev/react-bits), © David Haz, MIT + Commons Clause; "
        "modified. Not covered by this repository's MIT licence."
    )
    copies = sorted(p for p in reactbits.iterdir() if p.suffix in {".tsx", ".css"})
    assert {p.name for p in copies} >= {
        "BlurText.tsx",
        "CountUp.tsx",
        "DecryptedText.tsx",
        "RotatingText.tsx",
    }
    for copy in copies:
        first = copy.read_text(encoding="utf-8").splitlines()[0]
        assert header in first, f"{_rel(copy)} does not start with the React Bits header"

    notice = (ROOT / "NOTICE").read_text(encoding="utf-8")
    assert "site/src/components/reactbits/" in notice
    assert "NOT covered by this repository's MIT licence" in notice
    dockerignore = (ROOT / "Dockerfile.dockerignore").read_text(encoding="utf-8").splitlines()
    assert "site" in dockerignore


def test_site_build_output_is_ignored() -> None:
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    for entry in ("site/node_modules/", "site/dist/", "site/.astro/"):
        assert entry in gitignore
    assert (SITE / "package-lock.json").is_file(), "the lockfile is committed: CI runs npm ci"


def test_site_workflow_checks_pull_requests_and_deploys_main() -> None:
    workflow = (ROOT / ".github" / "workflows" / "site.yml").read_text(encoding="utf-8")
    # A release touches only pyproject.toml and worker_contract/version.py: a
    # path filter would leave the site on the old version.
    assert "paths:" not in workflow
    assert "paths-ignore:" not in workflow
    assert "branches: [main]" in workflow
    assert "workflow_dispatch:" in workflow
    assert "pull_request:" in workflow
    for step in ("npm ci", "npm run build", "npm test", "npm run check"):
        assert step in workflow
    assert "uses: withastro/action@v6" in workflow
    assert "path: ./site" in workflow
    assert "node-version: 24" in workflow
    assert "uses: actions/deploy-pages@v5" in workflow
    assert "name: github-pages" in workflow
    for permission in ("contents: read", "pages: write", "id-token: write"):
        assert permission in workflow

    package = (SITE / "package.json").read_text(encoding="utf-8")
    assert '"build": "astro build && pagefind --site dist"' in package
    assert "scripts/check-links.mjs" in package
    assert "scripts/check-js-budget.mjs" in package
    assert "160 * 1024" in (SITE / "scripts" / "check-js-budget.mjs").read_text(encoding="utf-8")
