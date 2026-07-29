---
name: investigate-a-codebase
description: map an unfamiliar repo — entry points, layers, and where things live
---

# Investigate a codebase

Tools: search_files, read_file, git_log, repo_state, delegate_analysis

1. `read_file` the README and the manifest (pyproject/package.json/
   go.mod) — name, entry points, dependencies.
2. `search_files` for main/serve/cli entry points; `read_file` the top
   of each. `git_log` for where recent work concentrates — the hot dirs
   are the real architecture.
3. Big repo → `delegate_analysis`: one analyst per subsystem, then
   synthesize one map.
4. Deliver: purpose, the 3-6 load-bearing directories with one line
   each, the request/data flow end to end, and the one file to read
   first. Anchor claims as path:line — a claim without an anchor is a
   guess and gets labeled as one.
