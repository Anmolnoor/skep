---
name: deck-widgets
description: author small no-build widgets for skep's own static deck UI
---

# Deck widgets (skep's own UI)

Tools: dispatch_run, read_file, search_files, get_run

The adaptation absorbing Hermes's petdex/tui-widgets: those decorated
Hermes's TUI; skep's equivalent surface is the no-build static deck
(`src/skep/supervisor/serve/static/` — plain ES modules + CSS tokens,
no bundler, no npm, fonts vendored).

1. Read the deck first: `app.js` conventions, the CSS token sheet,
   how existing panels mount. A widget that ignores the tokens looks
   like a sticker on the dashboard.
2. A widget = one ES module exporting a mount function + one CSS
   file using the existing tokens (no new colors, no new fonts, no
   external requests — the UI works offline).
3. Keep widgets read-only over existing API endpoints (`/api/...`
   reads); a widget wanting a new endpoint or a mutation is a feature,
   not a widget — plan it properly instead.
4. Ship as a coding-caste dispatch against the skep repo; the patch
   lands through the NORMAL human approval like any code change —
   decorating the supervisor never bypasses the supervisor.
5. Verify in the run: the module imports clean (node --check /
   browser import), CSS references only existing tokens.
