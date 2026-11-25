# Prerequisites: what to have ready before the first run

skep itself installs with one command, but it works with programs that are
already on your machine: Git, a sandbox, your project's test runner, and
whichever coding agent and GitHub tooling you choose to use. This page lists
all of them in one place, says which are required and which are optional, and
gives the install command and the check for each.

Read the short list first. Install what the first three sections name, then
add the optional sections for the features you want.

## The short list

| You need | For | Required? |
|---|---|---|
| macOS or Linux, Python 3.12 or newer, Git | skep itself | yes |
| `pipx` or `uv` (or `pip` inside a virtual environment) | installing skep | yes |
| A model provider you can reach, with the model you pick | live chat and skep's own coding worker | yes |
| A git repository with at least one commit | any coding run | yes |
| bubblewrap (Linux only) | the native sandbox: sandboxed runs, sandboxed re-verification, strict reads | yes, for sandboxed work |
| Your project's test runner (`uv`, `pytest`, `npm` or `make`) | the verify command that skep re-runs | yes, for a run skep confirms |
| ripgrep (`rg`) | the assistant's `search_files` tool | recommended |
| `gh`, signed in | opening, reviewing and merging pull requests | optional |
| `claude` or `codex`, with its login or key | running Claude Code or Codex as the worker | optional |
| Node.js with `npm` | the Codex CLI, `npm test` projects, a browser MCP server | optional |
| podman | a container backend for some worker runs | optional |

Nothing in the optional rows is needed for a first run. skep ships its own
coding worker, which uses the model you connect during `skep onboard`.

## 1. Always needed

**An operating system skep supports.** macOS or Linux.

**Python 3.12 or newer, and Git.**

```sh
python3 --version   # 3.12 or newer
git --version
```

**A way to install skep.** On Debian and Ubuntu the system Python refuses a
bare `pip install`, so install skep as an app:

```sh
sudo apt install pipx    # Fedora: sudo dnf install pipx; macOS: brew install pipx
pipx ensurepath          # puts ~/.local/bin on your PATH for new shells
source ~/.bashrc         # or open a new shell
pipx install skep
skep --version
```

`uv tool install skep` works the same way, and so does `pip install skep`
inside a virtual environment.

**A model provider.** `skep onboard` makes a test call to the model, so the
provider must be reachable, and the model you pick must be available on it,
before you start. You have two choices:

- **A local Ollama.** The Ollama server must be running and the model
  downloaded (`ollama pull <model>`). It normally needs no key. On a fresh
  home, with no provider named, onboarding looks for it at
  `http://localhost:11434`.
- **A hosted provider.** It needs its credential. For most providers that is
  an API key: either an environment variable, whose **name** onboarding asks
  for (never the key itself), or a key you store with
  `skep provider set-key`. Bedrock takes no key and signs with the AWS
  credentials in the environment. For `skep serve`, put a key variable in
  `~/.skep/serve.env` (one `NAME=value` line, file mode 0600): the daemon
  loads that file at every start.

`skep provider presets` lists the providers skep knows.
[`onboarding.md`](onboarding.md) covers the model step.

**A git repository to work in.** Onboarding registers a work directory
(default `~/workspace`) as a project. A coding run needs it to be a git
repository with at least one commit.

- If the directory does not exist, onboarding creates it and gives it a git
  baseline commit. You do nothing.
- If the directory exists and is not a git repository, onboarding leaves it
  as it is and says so. Make the baseline yourself. `git commit` needs an
  identity, so set one first on a new machine:

  ```sh
  git config --global user.name "Your Name"
  git config --global user.email "you@example.com"
  git init ~/workspace
  git -C ~/workspace commit --allow-empty -m "baseline"
  ```

The account that runs skep must be able to write to the repository's `.git`
directory and to the skep home (`~/.skep`).

**A free loopback port.** After its last step, onboarding connects to
`skep serve`, or starts it on port 8765 (or the port you configured), and
opens the chat.

## 2. The sandbox

skep runs workers inside the operating system's sandbox, and it re-runs your
test command inside the same sandbox.

**macOS.** Nothing to install. Seatbelt ships with the system.

**Linux.** Install bubblewrap:

```sh
sudo apt install bubblewrap   # Debian, Ubuntu
sudo dnf install bubblewrap   # Fedora
```

Then test that it can start:

```sh
bwrap --ro-bind / / --unshare-user --unshare-net true && echo BWRAP_OK || echo BWRAP_BLOCKED
```

Ubuntu 24.04 prints `BWRAP_BLOCKED` until you allow bubblewrap to create user
namespaces. The fix is one small AppArmor profile:
[`guide-ubuntu-server.md`](guide-ubuntu-server.md), section 3. `skep doctor`
runs skep's own, fuller probe and is the final check.

**"podman: unusable" and "docker: not on PATH" are not errors.** The sandbox
step of `skep onboard` lists every backend it looked for. On a Linux machine
with bubblewrap and no container tools it prints:

```text
  auto: usable — bubblewrap
  podman: unusable — podman is not on PATH
  none: usable — no sandbox ring
  docker: not on PATH
```

`auto` is the native backend (Seatbelt or bubblewrap), and it is the one to
have. Onboarding recommends the first usable choice in the order `auto`,
`podman`, `none`. Docker is reported but never used: skep has no Docker
backend.

**podman does not replace bubblewrap.** With `sandbox_backend=podman`, podman
runs the workers it can. A run that needs strict reads or a list of allowed
network hosts uses the native backend anyway, and sandboxed re-verification
always probes the native backend. So on Linux install bubblewrap whether or
not you also want podman.

**With no usable backend.** `skep onboard --sandbox none` lets onboarding
finish, and it skips the "work without asking" offer. skep's own worker can
then run in `workspace` mode (`--execution-mode workspace`), which is not
sandboxed. A sandbox-mode run still needs a usable backend, and an external
coding agent runs only in sandbox mode.

**A stripped-down host.** A normal desktop or server install has these; a
minimal container image may not. skep calls `ps` to find and stop worker
processes (package `procps`), `/bin/sh` to run your verify command, and
`/usr/bin/true` in the sandbox probe. The podman probe also uses `cat`.

## 3. Your project's test runner

skep confirms a run by re-running a test command itself, on a clean checkout.
The command a project fixes for that is its **pinned verify command**. It runs
on the machine skep runs on, so the tools it names must be installed there.

When you register a project and do not name a command (and the project has
none yet), skep picks one from what the repository contains. It tries these
rows in order, and a row counts only when its tool is on the `PATH`:

| The repository has | And the host has | Pinned command |
|---|---|---|
| a `Makefile` with a `test` target | `make` | `make test` |
| `pyproject.toml`, a `tests/` directory or a top-level `test_*.py` file, and `uv.lock` | `uv` | `uv run pytest` |
| `pyproject.toml` and a `tests/` directory or a top-level `test_*.py` file | `pytest` | `pytest` |
| a `package.json` whose `test` script is a real one, not `npm init`'s "no test specified" placeholder | `npm` | `npm test` |

If no row matches, the project is pinned to nothing. Re-verification then
re-runs the command the worker named, so a run can still be confirmed, but
an unpinned project is limited:

- its runs never land by themselves,
- it cannot be unlocked ("work without asking"),
- it cannot use an external coding agent.

So install the runner your projects use before you register them:

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh   # uv, into ~/.local/bin
sudo apt install nodejs npm                       # Node projects
sudo apt install make                             # Makefile projects
```

**Node projects need a lockfile.** When the pin runs `npm`, `npx`, `pnpm` or
`yarn` and `package.json` declares dependencies, skep installs them from the
lockfile before the test run. It needs `package-lock.json`,
`npm-shrinkwrap.json`, `pnpm-lock.yaml` or `yarn.lock` in the repository, and
that lockfile's package manager on the host. With no lockfile the re-run is
reported `unavailable`. If `pnpm` or `yarn` is a corepack launcher, cache the
version first: `corepack prepare <tool>@<version> --activate`.

**Where the runner is installed matters.** A project with strict reads sees
the system directories and the toolchain locations skep detects
(`~/.local/bin`, uv's Pythons and tools, `~/.cargo`, `~/.nvm`, `~/.pyenv`).
A runner installed somewhere else has to be added to the project's
`extra_read_paths`. [`sandboxing.md`](sandboxing.md) has the full list.

`uv` has a second use. Installing the pinned dependencies of a forged tool
needs `uv` on the `PATH`.

To pin a command yourself, or to change it:
[`guide-pin-verify-command.md`](guide-pin-verify-command.md).

## 4. Optional: an external coding agent

skep's own worker needs nothing beyond the model from section 1. Install an
agent only if you want Claude Code or Codex to do the coding.

`skep doctor` prints one line for each engine. `MISSING` means its program is
not on the `PATH`. For an engine you do not plan to use, that line is not a
problem.

| Engine | Program on `PATH` | How it signs in |
|---|---|---|
| `builtin` | none (skep's own worker) | the model provider from section 1 |
| `claude_code` | `claude` | `CLAUDE_CODE_OAUTH_TOKEN` (printed by `claude setup-token`) or `ANTHROPIC_API_KEY`, as an environment variable |
| `codex` | `codex` | `OPENAI_API_KEY` as an environment variable, or your ChatGPT plan login for a project that opts in |
| `acp:claude-agent-acp` | `claude-agent-acp` | the same as `claude_code` |
| `acp:codex-acp` | `codex-acp` | the same as `codex` |
| `aider`, `pi` | `aider`, `pi` | their own provider's key; advanced, never recommended |

Install the Codex CLI into a user-owned npm prefix, so it needs no sudo:

```sh
mkdir -p ~/.npm-global
npm config set prefix ~/.npm-global
npm install -g @openai/codex
export PATH=$HOME/.npm-global/bin:$PATH   # also add it to your shell profile
codex --version
```

Install Claude Code by Anthropic's own instructions. Then, from the same
shell or service that runs skep, check that `claude --version` answers and
that `skep doctor` no longer says `MISSING` (section 7).

An external agent also needs these from skep. A run is refused before it
starts when one is missing:

1. **A usable sandbox** (section 2). The agent's own commands do not pass
   skep's capability layer, so the sandbox is its only wall.
2. **A real pinned verify command** on the project (section 3). A pin that
   skep knows proves nothing, such as `true` or `git diff --check`, is
   refused too.
3. **Its sign-in.** The agent's normal login (the keychain,
   `~/.claude/.credentials.json`, `~/.codex/auth.json`) is hidden from every
   sandboxed worker. For Claude Code and for Codex with an API key, export
   the variable in the shell you run `skep run` from, or put it in
   `~/.skep/serve.env` for `skep serve`. Codex can instead use your ChatGPT
   plan login on a project that opted in:
   [`codex-chatgpt-login.md`](codex-chatgpt-login.md).
4. **For the two `acp:` engines only: strict reads** on the project
   (`skep project setup <repo> --strict-reads`), on a backend that can
   enforce them.

With the program installed and no credential, `skep doctor` says
`NO AUTH ENV` and names the variables. The full walk-through is
[`guide-unattended-run.md`](guide-unattended-run.md).

## 5. Optional: GitHub

Runs land on a local branch (`skep/<task_id>` unless you name another) and
need no GitHub access. You need the GitHub CLI only for pull requests.

```sh
sudo apt install gh     # Fedora: sudo dnf install gh; macOS: brew install gh
gh auth login
gh auth setup-git       # lets `git push` use the same login over HTTPS
gh auth status
```

In every case the repository's `origin` remote must be a `github.com`
repository. Then, by task:

- **Opening a pull request** (`skep review --approve --pr`, `skep pr open`).
  Your git credentials must be able to push a branch to `origin`: skep pushes
  the landed branch with `git push`, then calls `gh pr create`. The base
  branch must exist on GitHub. For a repository that was created empty, skep
  offers a `push_baseline` card, which only creates the missing base branch.
- **Reviewing a pull request at its exact head.** skep reads the PR through
  `gh` and fetches its head and base from `origin` with git, so both need
  read access.
- **Merging through skep.** This needs a clean, confirmed review of the PR's
  current head and base, your approval, and permission to merge on GitHub.

Without `gh`, `skep review --approve --pr` still lands the branch and prints
`note: gh CLI not found — push the branch and open the PR manually`.

## 6. Optional: everything else

| Feature | What it needs |
|---|---|
| The assistant's `search_files` tool | ripgrep: `sudo apt install ripgrep` (Fedora: `sudo dnf install ripgrep`; macOS: `brew install ripgrep`). Without it the tool answers `ripgrep (rg) is not installed on the host`. |
| Browser automation | `npx` (it ships with npm), to run the Playwright MCP server. See [`browser.md`](browser.md). |
| Office and PDF documents | the `documents` extra: `pipx install --force "skep[documents]"` |
| OCR | the `ocr` extra, and the `tesseract` system program (`sudo apt install tesseract-ocr`, `brew install tesseract`) |
| Spoken replies | the `voice` extra |
| Schedules that fire on time | a machine that stays on, with `skep serve` running as a service. See [`guide-ubuntu-server.md`](guide-ubuntu-server.md). |
| Approving from your phone | a Telegram bot token. See [`mobile.md`](mobile.md) and [`daily-driver-setup.md`](daily-driver-setup.md). |

skep's own `/update` installs plain `skep==<version>`, with no extras. After
an update, install the extra again.

## 7. The service's `PATH` is not your shell's `PATH`

skep looks for every program above on the `PATH` of the process that runs it.
In a terminal that is your shell's `PATH`. Under systemd it is whatever the
unit sets, and a tool you installed into `~/.local/bin` or `~/.npm-global/bin`
is invisible unless the unit names that directory:

```ini
[Service]
Environment=PATH=%h/.local/bin:%h/.npm-global/bin:/usr/local/bin:/usr/bin:/bin
```

If a program works in your shell and a run says it is missing, check this
line first. After you change it, run `systemctl --user daemon-reload` and
restart the service.

## Install everything on Ubuntu 24.04

This is the full set for a machine that will run sandboxed coding runs, open
pull requests, and can use Codex. Leave out the lines for features you do not
want.

```sh
# as a user with sudo
sudo apt update
sudo apt install -y git pipx bubblewrap ripgrep make gh nodejs npm procps
# then the AppArmor profile for bwrap: guide-ubuntu-server.md, section 3

# as the user that runs skep
pipx ensurepath          # then open a new shell
pipx install skep
curl -LsSf https://astral.sh/uv/install.sh | sh
gh auth login && gh auth setup-git
```

On Fedora the same packages install with `dnf`
(`sudo dnf install git pipx bubblewrap ripgrep make gh nodejs npm`). On macOS,
`brew install pipx git ripgrep gh node uv` covers the same list, and there is
no sandbox package to install.

## Check it

```sh
skep doctor
```

| The line says | It means | Do this |
|---|---|---|
| `sandbox: ready` | the native sandbox probe passed | nothing |
| a sandbox line with `bwrap was not found on PATH` | bubblewrap is not installed | section 2 |
| any other sandbox line that is not `ready` | the probe did not pass; the line gives the reason | read the reason; on Ubuntu 24.04 it is usually the AppArmor step in section 2 |
| `engine <name>: MISSING` | that agent's program is not on the `PATH` | section 4, or ignore it for an engine you do not use |
| `engine <name>: NO AUTH ENV` | the program is there, its credential is not | section 4 |
| a project under **Projects needing upgrade** | it runs unsandboxed, pins no verify command, or reads the whole disk | run the `skep project setup …` line doctor prints. If it ends in `'<your test command>'`, install the runner (section 3) and put your command there. If doctor cannot find the checkout, it says what to run once you have one. |
| `provider: ready` | a live provider answered a real completion (or the `mock` provider is configured) | nothing |

`skep onboard --check` runs the same report plus the onboarding checks, and
exits 0 only when every check is ready.
