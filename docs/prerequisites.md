# Prerequisites: what to have ready before the first run

skep itself installs with one command, but it works with programs that are
already on your machine: Git, a sandbox, your project's test runner, and
whichever coding agent and GitHub tooling you choose to use. This page lists
all of them in one place, says which are required and which are optional, and
gives the install command and the check for each.

Read the short list first. Install what the first two groups name, then add
the optional groups for the features you want.

## The short list

| You need | For | Required? |
|---|---|---|
| macOS or Linux, Python 3.12 or newer, Git | skep itself | yes |
| `pipx` or `uv` | installing skep as an app | yes |
| A model provider and its key | the chat assistant and skep's own coding worker | yes |
| A git repository with at least one commit | any coding run | yes |
| bubblewrap (Linux only) | the sandbox | yes, for sandboxed runs |
| Your project's test runner (`uv`, `pytest`, `npm` or `make`) | the pinned verify command that skep re-runs | yes, for a confirmed run |
| ripgrep (`rg`) | the assistant's `search_files` tool | recommended |
| `gh`, signed in | opening, reviewing and merging pull requests | optional |
| `claude` or `codex`, with a credential | running Claude Code or Codex as the worker | optional |
| Node.js with `npm` | the Codex CLI, `npm test` projects, a browser MCP server | optional |
| podman | a container sandbox instead of the native one | optional |

Nothing in the optional rows is needed for a first run. skep ships its own
coding worker, which uses the model you connect during `skep onboard`.

## 1. Always needed

**An operating system skep supports.** macOS or Linux.

**Python 3.12 or newer, and Git.**

```sh
python3 --version   # 3.12 or newer
git --version
```

**`pipx` or `uv`, to install skep as an app.** On Debian and Ubuntu the system
Python refuses a bare `pip install`, so use one of these:

```sh
sudo apt install pipx    # Fedora: sudo dnf install pipx; macOS: brew install pipx
pipx ensurepath          # puts ~/.local/bin on your PATH for new shells
source ~/.bashrc         # or open a new shell
pipx install skep
skep --version
```

`uv tool install skep` works the same way.

**A model provider.** `skep onboard` makes a test call to the model, so the
provider must be reachable and its key must exist before you start. You have
two choices:

- A local Ollama. On a fresh home, with no provider named, onboarding looks
  for one at `http://localhost:11434`.
- A hosted provider. Put its API key in an environment variable. Onboarding
  asks for the **name** of that variable, never the key. For `skep serve`, put
  the line in `~/.skep/serve.env` (for example `OLLAMA_API_KEY=...`, file mode
  0600): the daemon loads that file at every start.

`skep provider presets` lists the providers skep knows.
[`onboarding.md`](onboarding.md) covers the model step.

**A git repository to work in.** Onboarding registers a work directory
(default `~/workspace`) as a project and creates the directory if it is
missing. A coding run needs it to be a git repository with at least one
commit. For a new, empty directory:

```sh
git init ~/workspace
git -C ~/workspace commit --allow-empty -m "baseline"
```

## 2. The sandbox

skep runs workers inside the operating system's sandbox. Without one,
`skep onboard` can still finish with `--sandbox none`, but then nothing
confines the worker process, external coding agents cannot run at all, and
"work without asking" is unavailable.

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
[`guide-ubuntu-server.md`](guide-ubuntu-server.md), section 3.

**"podman: unusable" and "docker: not on PATH" are not errors.** The sandbox
step of `skep onboard` lists every backend it looked for:

```text
  auto: usable — bubblewrap
  podman: unusable — podman is not on PATH
  none: usable — no sandbox ring
  docker: not on PATH
```

You need one usable backend, and `auto` (the native one) is the recommended
choice. podman is an opt-in alternative. Docker is reported but never used:
skep has no Docker backend. Install podman only if you want container
isolation instead of the native sandbox (`sudo apt install podman`).

**`ps`.** skep uses `ps` to find and stop worker processes. Desktop and server
installs have it. A minimal container image may not: install `procps`.

## 3. Your project's test runner

skep confirms a run by re-running your project's test command itself, on a
clean checkout. That command is the project's **pinned verify command**. It
runs on the machine skep runs on, so the tools it names must be installed
there.

When you register a project, skep picks the pin from what the repository
contains, and only when the tool is on the `PATH`:

| The repository has | And the host has | Pinned command |
|---|---|---|
| a `Makefile` with a `test` target | `make` | `make test` |
| `pyproject.toml`, tests, and `uv.lock` | `uv` | `uv run pytest` |
| `pyproject.toml` and tests | `pytest` | `pytest` |
| a `package.json` with a `test` script | `npm` | `npm test` |

The rows are tried in that order. If none matches, the project is pinned to
nothing. A project with no pin still runs, but:

- its runs never land by themselves,
- it cannot be unlocked ("work without asking"),
- it cannot use an external coding agent.

So install the runner your projects use before you register them:

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh   # uv, into ~/.local/bin
sudo apt install nodejs npm                       # Node projects
sudo apt install make                             # Makefile projects
```

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

| Engine | Program on `PATH` | Credential, as an environment variable |
|---|---|---|
| `builtin` | none (skep's own worker) | the model provider from section 1 |
| `claude_code` | `claude` | `CLAUDE_CODE_OAUTH_TOKEN` (printed by `claude setup-token`) or `ANTHROPIC_API_KEY` |
| `codex` | `codex` | `OPENAI_API_KEY`, or a ChatGPT plan login for a project that opts in |
| `acp:claude-agent-acp` | `claude-agent-acp` | the same as `claude_code` |
| `acp:codex-acp` | `codex-acp` | the same as `codex` |
| `aider`, `pi` | `aider`, `pi` | advanced; never recommended |

Install the Codex CLI into a user-owned npm prefix, so it needs no sudo:

```sh
mkdir -p ~/.npm-global
npm config set prefix ~/.npm-global
npm install -g @openai/codex
~/.npm-global/bin/codex --version
```

Install Claude Code by Anthropic's own instructions, then check that
`claude --version` answers.

An external agent also needs three things from skep, and a run is refused
before it starts when one is missing:

1. **A usable sandbox** (section 2). The agent's own commands do not pass
   skep's capability layer, so the sandbox is its only wall.
2. **A pinned verify command** on the project (section 3).
3. **Its credential in the environment.** The agent's normal login (the
   keychain, `~/.claude/.credentials.json`, `~/.codex/auth.json`) is hidden
   from every sandboxed worker. Export the variable in the shell you run
   `skep run` from, or put it in `~/.skep/serve.env` for `skep serve`.

With the program installed and no credential, `skep doctor` says
`NO AUTH ENV` and names the variables. The full walk-through is
[`guide-unattended-run.md`](guide-unattended-run.md). The ChatGPT plan login
is in [`codex-chatgpt-login.md`](codex-chatgpt-login.md).

## 5. Optional: GitHub

Runs land on a local `skep/<task_id>` branch and need no GitHub access. You
need the GitHub CLI only for pull requests: `skep review --approve --pr`,
`skep pr open`, reviewing a PR at its exact head, and merging through skep.

```sh
sudo apt install gh     # Fedora: sudo dnf install gh; macOS: brew install gh
gh auth login
gh auth setup-git       # lets `git push` use the same login over HTTPS
gh auth status
```

Three more things must be true for a pull request to open:

- The repository's `origin` remote is a `github.com` repository.
- Your git credentials can push a branch to it. skep pushes the `skep/...`
  branch with `git push`, then calls `gh pr create`.
- The base branch exists on GitHub. For a repository that was created empty,
  skep offers a `push_baseline` card to create it. It never updates a default
  branch that already exists.

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
| `sandbox: ready` | the sandbox works | nothing |
| `bwrap was not found on PATH` | bubblewrap is not installed | section 2 |
| a sandbox line that is not `ready` and does not say `bwrap was not found` | bubblewrap is installed but the host stops it | run the `bwrap` test in section 2; on Ubuntu 24.04 it is the AppArmor step |
| `engine <name>: MISSING` | that agent's program is not on the `PATH` | section 4, or ignore it for an engine you do not use |
| `engine <name>: NO AUTH ENV` | the program is there, its credential is not | section 4 |
| a project under **Projects needing upgrade** | it runs unsandboxed, pins no verify command, or reads the whole disk | run the `skep project setup …` line doctor prints; if it ends in `'<your test command>'`, install the runner (section 3) and put your command there |
| `provider: ready` | the model answered a real completion | nothing |

`skep onboard --check` runs the same report plus the onboarding checks, and
exits 0 only when every check is ready.
