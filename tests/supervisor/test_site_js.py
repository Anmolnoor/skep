"""docs-site: the site's pure build helpers run under ``node --test``.

``site/src/lib/*.mjs`` (link rewriter, README nav parser, changelog parser,
doc-slug map, fact parsers) import node builtins only, so the suite needs no
``npm ci`` and no ``site/node_modules``: plain node runs it, the same way
``test_web_js.py`` runs ``tests/web``. Without node a local run skips; under
CI (GitHub sets ``CI``) a missing node is a failure, never a silent skip.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

SITE_LIB = Path(__file__).resolve().parents[2] / "site" / "src" / "lib"


def test_site_lib_js_suite() -> None:
    node = shutil.which("node")
    if node is None:
        if os.environ.get("CI"):
            pytest.fail("node is required in CI to run site/src/lib/*.test.mjs")
        pytest.skip("node not installed — site/src/lib tests need it")
    files = sorted(str(p) for p in SITE_LIB.glob("*.test.mjs"))
    assert files, "no site/src/lib/*.test.mjs found"
    proc = subprocess.run(
        [node, "--test", *files], capture_output=True, text=True, timeout=120, check=False
    )
    assert proc.returncode == 0, proc.stdout[-4000:] + proc.stderr[-2000:]


def test_site_lib_helpers_import_no_npm_package() -> None:
    """The helpers run before ``npm ci``: every import is a node builtin or a
    sibling file. (``src/plugins/`` is where npm imports are allowed.)"""
    imports = re.compile(
        r"""^\s*(?:import|export)\b[^'"]*\bfrom\s+['"]([^'"]+)['"]""", re.MULTILINE
    )
    modules = sorted(SITE_LIB.glob("*.mjs"))
    assert modules
    for module in modules:
        for target in imports.findall(module.read_text(encoding="utf-8")):
            assert target.startswith(("node:", "./")), (
                f"{module.name} imports {target!r}: lib helpers take node builtins only"
            )
    # Each tested helper has its test file.
    for name in ("links", "nav", "changelog", "slugs", "facts"):
        assert (SITE_LIB / f"{name}.test.mjs").is_file()
