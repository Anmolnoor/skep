"""Write site/src/lib/slash-deck.json: the web command deck and the
terminal-only names, from the one registry (serve/commands.py). The docs site
builds with node and cannot import it; tests/supervisor/test_site_facts.py
holds the file against the registry."""

import json
from pathlib import Path

from skep.cli_chat import CLI_ONLY
from skep.supervisor.serve.commands import web_deck

OUT = Path(__file__).resolve().parents[1] / "site" / "src" / "lib" / "slash-deck.json"
OUT.write_text(
    json.dumps({"web": web_deck()["commands"], "cli_only": sorted(CLI_ONLY)}, indent=2) + "\n",
    encoding="utf-8",
)
