# Codex runs on a ChatGPT plan login (ES-F120–F129)

The `codex` engine authenticates from `OPENAI_API_KEY` in the environment
the worker inherits. Someone with only a ChatGPT plan has no key, and the
sandbox masks the Codex CLI's own login file (`~/.codex/auth.json`, W33).
This note records how a codex run may use the plan login instead, and why
it is built the way it is.

Invariants touched: **I5** (no new permission system: the opt-in is a
project-policy key resolved like every other), **I8** (the card, the run
record and `skep doctor` say which login a run used, never a value),
**I9** (every refusal names the way forward), **I12** (the network pin
gains exactly the host this mode needs). I1–I4 are untouched: the engine
stays forced into the sandbox, still needs a pinned `verify_command`, and
the patch still lands only through a human approval.

## How the Codex CLI uses and refreshes `auth.json`

Read from the Codex CLI source at tag `rust-v0.167.2` (the version
installed when this was built), `codex-rs/login/src/`:

- `auth/storage.rs` (`AuthDotJson`): the file holds `auth_mode`,
  `OPENAI_API_KEY`, `tokens` (`id_token`, `access_token`,
  `refresh_token`, `account_id`, ...) and `last_refresh`.
- `auth/manager.rs:3528` (`proactive_refresh_reason`): a ChatGPT login
  (`auth_mode: "chatgpt"`) is refreshed when the access token expires
  within 5 minutes (`:222`), or when `last_refresh` is older than 8 days
  (`:221`). A 401 also triggers a refresh (`UnauthorizedRecovery`).
- `auth/manager.rs:3612` (`refresh_and_persist_chatgpt_token`): the
  refresh **calls the token endpoint first** (`:3630`) and only then
  writes the new tokens back to `auth.json` (`persist_tokens`, `:1726`).
- `auth/storage.rs:281` + `auth/storage_file.rs:34`: a read-only
  `auth.json` makes the *write* fail ("credential file is read-only"),
  but by then the refresh token has already been spent at the server.
- `auth/manager.rs:225`: refresh tokens are single-use. A reused one
  fails with "your refresh token was already used. Please log out and
  sign in again."
- `auth/manager.rs:469`, `:1933`, `:3446`: a file whose `auth_mode` is
  `chatgptAuthTokens` (tokens supplied by an external host) loads as
  `CodexAuth::ChatgptAuthTokens`, uses in-memory storage (it is never
  written back), and its refresh is a no-op: Codex never contacts the
  token endpoint for it. `from_external_chatgpt_tokens` (`:899`) is
  Codex's own constructor for this mode: access token plus account id,
  empty refresh token.
- `model-provider-info/src/lib.rs:86`: ChatGPT-mode requests go to
  `chatgpt.com` (`/backend-api/codex`), not `api.openai.com`.

The access token in a fresh login lives about ten days.

## The danger

Handing a run a copy of the whole file means the run holds the
operator's refresh token. If Codex refreshes inside the sandbox, the
server rotates the token: the run's copy gets the new one, the
operator's own file keeps the spent one, and the operator's next Codex
refresh fails with "already used". The operator is logged out. Making
the copy read-only does not help, because the token is spent before the
write is attempted. Copying a refreshed file back at the end of the run
(the first sketch of W37) narrows the window but still breaks on a
crash, a kill or two runs at once.

## The design

The refresh token never leaves the host.

1. The supervisor reads the operator's `$CODEX_HOME/auth.json` (default
   `~/.codex`). It never writes it.
2. For a codex run that opted in and has no `OPENAI_API_KEY`, it writes
   a **new** file in Codex's `chatgptAuthTokens` mode: the access token,
   the account id, an empty refresh token, mode 0400. Codex never
   refreshes this mode, so nothing a run does can rotate or revoke the
   operator's login. The file lives in the run's audit dir
   (`<home>/supervisor/audit/<task>/codex-login.json`), which every
   worker's sandbox masks; the run's redirected `CODEX_HOME`
   (`<workspace>/.toolchain/codex`, 0700, v106-F1) holds only an
   `auth.json` link to it, and that one run's sandbox binds the file back
   read-only after the masks. A copy inside the worktree would be
   readable by every other sandboxed run on the machine, whatever its
   engine or project: worktrees are not masked.
3. The access token must outlive the run's wall-clock budget plus 5
   minutes, or the dispatch refuses before any run record exists and
   says how to refresh on the host (use `codex` once, or `codex login`;
   the Codex CLI refreshes its own file, under its own rules).
4. The run's network pin gets `chatgpt.com:443` instead of
   `api.openai.com:443`: the one host this mode talks to.
5. The file is deleted when the run ends (the `finally` of
   `run_task`), including runs whose worktree is kept for a retry.
   `.toolchain/` (and so the link) is excluded from every patch.
6. The real login file stays masked from every worker (W33), wherever it
   is: `~/.codex/auth.json`, or `$CODEX_HOME/auth.json` when the operator
   set `CODEX_HOME` (the file skep reads is the file it masks). The builtin worker, `claude_code`, `aider`, `pi` and every
   other codex run never get the run file: it is written only for an
   opted-in `codex` run and only that run's sandbox can read it. The
   copy is on the secret floor too (`codex-login.json`, and the link to
   it by its realpath): the Queen's `read_file`, `search_files` and host
   shell refuse it, and no card lifts that.

## Opting in

Off by default. A project-policy key, `codex_chatgpt_login` (bool):

- CLI: `skep project setup <repo> --strategy <strategy> --phase <phase>
  --codex-chatgpt-login` (`--no-codex-chatgpt-login` turns it off again).
  A repeated setup keeps every stored key but needs the strategy again, and
  `--phase` defaults to `build`, so name an existing project's own strategy
  and phase (`skep project show <id>`); the NO AUTH ENV refusal prints the
  exact line for the repo it refused.
- chat: `setup_project ... codex_chatgpt_login=true` (a HIGH confirmation
  card: it holds once and is never confirmable from a messenger channel);
  `codex_chatgpt_login=false` turns it off

`OPENAI_API_KEY`, when set, still wins: the plan login is used only when
no key is present.

## What says so

- The dispatch card and manifest: "uses your ChatGPT plan login (Codex)".
- The run record (the `dispatched` transition) and the run's
  `worker.log`: `codex_auth=chatgpt_login`. Never a value. Not the policy
  decision log (`<audit>/policy/decisions.jsonl`): that is the ledger of
  `decide()` verdicts, its replay counts every row as one, and a login
  choice is not a policy verdict.
- `skep doctor`: whether a ChatGPT login is saved, its plan and expiry
  when the token names them, which projects opted in, and what a codex
  run would use.
- The NO AUTH ENV refusal names the option when a ChatGPT login exists.

## Failure modes

- **The token expires or is revoked mid-run** (signed out elsewhere,
  password change): Codex gets a 401, cannot refresh, and the run fails.
  The operator's login is unaffected beyond whatever revoked it.
- **The token is too close to expiry at dispatch**: refused up front,
  with the refresh hint.
- **The supervisor dies mid-run**: the `finally` does not run and the
  0400 file stays in the run's audit dir, which no worker can read,
  until the operator removes it. It holds an access token only (at most
  ~10 days of life), never the refresh token.
- **The run reads its own token**: the codex process and the commands it
  runs can read the file, exactly as they can read `OPENAI_API_KEY` in
  the key mode. The network pin limits where it can be sent to
  `chatgpt.com`.
- **Codex changes the format**: `chatgptAuthTokens` is a documented mode
  of Codex's app-server protocol. If a later Codex stops loading it from
  disk, the run fails with Codex's own "not logged in" error, the
  operator's login is untouched, and the API-key mode still works.
