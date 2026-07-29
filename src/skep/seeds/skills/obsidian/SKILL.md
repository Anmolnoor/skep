---
name: obsidian
description: read, search, and create notes in the user's Obsidian vault
---

# Obsidian vault

Tools: sync_notes, list_notes, search_files, read_file, add_note

skep's Obsidian bridge is the notes sync (obsidian.py) — the vault is
also just markdown on disk, which the file tools read.

1. Where is it: the configured vault path (ask once; remember it). The
   vault counts as an operator root, so `read_file`/`search_files` read
   it directly — search by filename glob or ripgrep content.
2. Reading/answering: `search_files` the vault for the topic,
   `read_file` the hits, answer with [[wikilinks]] intact so the user
   can jump.
3. Writing: `add_note` + `sync_notes` pushes through the sync lane;
   direct vault writes are a worker dispatch (the vault is the user's
   life work — never edit it outside the governed lanes).
4. Respect the vault's own conventions (folders, frontmatter, daily
   note format) — read a sibling note before creating one.
