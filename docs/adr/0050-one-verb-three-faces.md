# ADR 0050 — one verb, three faces

**Status:** accepted (v104)
**Supersedes:** nothing. **Related:** ADR 0019 (the model never holds the
trigger), ADR 0049 (the caste registry).

## Context

skep exposes supervisor verbs on three surfaces: the chat tool surface the
Queen calls, the REST API the web UI calls, and the CLI the operator types.
Nothing has ever related them, so they drift — and they have drifted in the
same direction four times:

| version | the verb with no operator writer |
|---|---|
| v94-F5 | `coding_engine` — the Queen could set it, `project setup` could not |
| v100-F9 | `verify_command` — same |
| v101-F13 | `repo_slug` binding — chat wrote two kinds, REST three, CLI **one** |
| v104 | the whole branch and pull-request family — seven verbs, chat only |

Each was fixed one key at a time, and each fix was found the same way: an
operator got stuck and a field test recorded it. v104 was found by the
*previous round's own acceptance* — v103 shipped `merge_branch` and then had
to reach it through `uv run python -c` and open its pull request with a raw
`gh pr create`, because neither verb had a CLI face.

The direction of the drift is what makes it a defect rather than an
inconvenience. **I5 says one authorization boundary. It does not say the
operator gets the narrow half of it.** A boundary that is wider for a small
model in a chat box than for the human who owns the machine is not one
boundary; it is two, with the human on the weaker side. And every operation
the operator has to perform by hand — outside skep, in a terminal — is one
the audit trail never sees (I8) and the policy engine never gates.

## Decision

**A supervisor verb that mutates state is reachable from the chat tool
surface AND from at least one operator surface — the CLI or the REST API.**

Read verbs are exempt. The asymmetry that matters is authority, not
convenience: an operator who cannot *see* something can go and look, but an
operator who cannot *do* something is genuinely less powerful than the model
acting on their behalf.

Exceptions are enumerated, not assumed. `CHAT_ONLY` in
`tests/supervisor/test_surface_parity.py` maps each exempt verb to a one-line
reason, and an entry without a reason fails the test. The categories that
legitimately qualify:

- **Verbs that lend the Queen the operator's own standing.** `run_shell`,
  `read_file`, `search_files` exist so the *model* can act under the operator
  policy. The operator already has a shell.
- **Verbs whose subject is the chat.** `set_personality`, `clarify`,
  `remember` — there is no chat to configure from a terminal.
- **Verbs whose operator face is the command deck.** `/persona`,
  `/browser`, `/resume` are client-side and invisible to a source-level
  detector, but they are a real operator surface.

`tests/supervisor/test_surface_parity.py` is the enforcement, and
`KNOWN_GAPS` is the ledger of verbs a round has accepted and not yet closed —
non-empty only while a round is in flight.

## Consequences

A new mutating verb costs one CLI subcommand or one route. That is the price
of not building the fifth instance of this bug, and it is small: the v104
faces are thin wrappers that add no logic and no validation — the refusals
stay in `serve/actions.py`, so the CLI and the chat verb cannot disagree about
what is allowed. A second copy of "never the default branch" in the CLI would
itself be the shadow permission system I5 rejects.

CLI verbs are **not** carded. A typed command is the operator's decision (I7),
the same rule that makes `skep review <id> --approve` act immediately. The
card exists because a *model* proposed the action.

## Rejected alternatives

**Generate the CLI from the tool specs.** It would give `delegate_analysis` a
command nobody wants, and argparse help written by a JSON schema is worse than
help written for a human. The parity test checks that a face *exists*; it does
not dictate the shape, because the shapes genuinely differ — `dispatch_run` is
`skep run`, and that is the right name for it.

**Require all three faces.** REST exists for the web UI, which already reaches
most verbs through the chat card path. Demanding a route per verb would add
surface nobody calls, which is the speculative generality this project rejects
elsewhere. One reachable operator surface is what I5 actually requires.

**Fix it in code review instead of a test.** That is what was tried, implicitly,
for four rounds.
