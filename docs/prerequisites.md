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
| bubblewrap (Linux only) | the native sandbox, which strict reads, a list of allowed network hosts and re-verification (by default) all use | yes, for the full sandboxed workflow |
| Your project's test runner (`uv`, `pytest`, `npm` or `make`) | the verify command that skep re-runs | yes, for a run skep confirms |
| ripgrep (`rg`) | the assistant's `search_files` tool | recommended |
| `gh`, signed in | opening, reviewing and merging pull requests | optional |
| `claude` or `codex`, with its login or key | running Claude Code or Codex as the worker | optional |
| Node.js with `npm` | the Codex CLI, `npm test` projects, a browser MCP server | optional |
| podman | a container sandbox for worker runs that need neither strict reads nor a list of allowed hosts | optional |

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

**A model provider.** For live use, `skep onboard` tests the model you pick
on the endpoint you pick, so the endpoint must be reachable and the model
available on it before you start. (`--provider mock` makes no call. It is a
dry run, not a working assistant.) The endpoint can be:

- **A local Ollama.** The Ollama server must be running and the model
  downloaded (`ollama pull <model>`). It normally needs no key. On a fresh
  home, with no provider named, onboarding looks for it at
  `http://localhost:11434`.
- **A hosted provider, or an OpenAI-compatible endpoint** that answers both
  a model-list request and a test completion for the model you pick. Supply
  its credential if it needs one. For an API key, export it in the shell that runs
  `skep onboard` and give onboarding the variable's **name** (it never asks
  for the key itself). The other way is to register the provider first
  (`skep provider add`) and then store the key with `skep provider set-key`.
  Bedrock takes no key and signs with the AWS credentials in the
  environment. For `skep serve`, put a key variable in `~/.skep/serve.env`
  (one `NAME=value` line, file mode 0600): the daemon loads that file at
  every start.

`skep provider presets` lists the providers skep knows.
[`onboarding.md`](onboarding.md) covers the model step.

**A git repository to work in.** Onboarding registers a work directory
(default `~/workspace`) as a project. A coding run needs it to be a git
repository with at least one commit.

- If the directory does not exist, onboarding creates it and gives it a git
  baseline commit. You do nothing.
- If the directory exists and is not a git repository, onboarding leaves it
  as it is and says so. Make the baseline yourself, with the files you want
  skep to work on in it: a worker starts from what is committed. `git commit`
  needs an identity, so set one first on a new machine:

  ```sh
  git config --global user.name "Your Name"
  git config --global user.email "you@example.com"
  cd ~/workspace
  git init
  git add -A                    # review what this stages first
  git commit -m "baseline"      # add --allow-empty for a folder with no files
  ```

The account that runs skep must be able to write to the repository's `.git`
directory and to the skep home (`~/.skep`).

**A free loopback port.** After its last step, onboarding connects to
`skep serve`, or starts it on port 8765 (or the port you configured), and
opens the chat.

## 2. The sandbox

skep runs workers inside the operating system's sandbox. By default it also
re-runs your test command inside the native sandbox, whichever backend the
worker used.

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
coding agent runs only in sandbox mode. By default re-verification needs the
native backend too: without one it reports `unavailable` instead of running
your test command on the bare host, so the run is not confirmed.

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
| a `Makefile` with a line that begins `test:` | `make` | `make test` |
| `pyproject.toml`, a `tests/` directory or a top-level `test_*.py` file, and `uv.lock` | `uv` | `uv run pytest` |
| `pyproject.toml` and a `tests/` directory or a top-level `test_*.py` file | `pytest` | `pytest` |
| a `package.json` with a non-empty `scripts.test` that does not contain `no test specified` (the `npm init` placeholder) | `npm` | `npm test` |

If no row matches, the project is pinned to nothing. Re-verification then
re-runs the command the worker named, so a run can still be confirmed, but
an unpinned project is limited:

- its runs never land by themselves,
- it cannot be unlocked ("work without asking"),
- it cannot use an external coding agent,
- a pull request review on it is never confirmed: a PR head is re-verified
  only with a pinned command.

So install the runner your projects use before you register them:

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh   # uv, into ~/.local/bin
sudo apt install nodejs npm                       # Node projects
sudo apt install make                             # Makefile projects
```

**Node projects need a lockfile.** When the pin runs `npm`, `npx`, `pnpm` or
`yarn` and `package.json` declares dependencies, skep tries to install them
from the lockfile before the test run. It needs `package-lock.json`,
`npm-shrinkwrap.json`, `pnpm-lock.yaml` or `yarn.lock` in the repository, and
that lockfile's package manager on the host. With no lockfile, or when the
install fails, the re-run is reported `unavailable`. The `yarn.lock` install
uses Yarn Classic's flags, so Yarn 2 and newer fails there. If `pnpm` or
`yarn` is a corepack launcher, cache the version first:
`corepack prepare <tool>@<version> --activate`.

**Where the runner is installed matters.** A project with strict reads can
read the run's own files, the system directories and the toolchains skep
detects (among them `~/.local/bin`, uv's Pythons and tools, `~/.cargo`,
`~/.nvm`, `~/.pyenv`, and the `uv` program found on skep's `PATH`). For a
tool or dependency outside those, add its absolute path to the project's
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

**Where the program is installed matters.** A project with strict reads (the
default for a new sandboxed project) reads only what section 3 lists. An
agent installed outside that passes `--version` and `skep doctor` on the
host, and the worker still cannot start it until its directory is in the
project's `extra_read_paths`.

- **Claude Code.** Install it by Anthropic's own instructions. Their
  installer's default places, `~/.local/bin` and `~/.local/share/claude`, are
  both in the strict-read set.
- **Codex.** Install it system-wide, or with a Node that lives under
  `~/.nvm`:

  ```sh
  sudo npm install -g @openai/codex   # system npm, root-owned prefix
  npm install -g @openai/codex        # Node from nvm: as the skep user, no sudo
  codex --version
  ```

  A user npm prefix such as `~/.npm-global` is outside the default
  strict-read set. If you use one, add that directory to the project's
  `extra_read_paths`. There is no CLI flag for it: chat and REST set it
  through `policy_overrides`.

Then, from the same shell or service that runs skep, check that the program
answers `--version` and that `skep doctor` no longer says `MISSING`
(section 7).

An external agent also needs these from skep. A run is refused before it
starts when one that applies to its engine is missing:

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
   [`codex-chatgpt-login.md`](codex-chatgpt-login.md). skep checks the
   sign-in before dispatch for `claude_code`, `codex` and the two `acp:`
   engines. `aider` and `pi` declare no required variable, so skep cannot
   check theirs.
4. **For the two `acp:` engines only: strict reads** on the project
   (`skep project setup <repo> --strict-reads`), on a backend that can
   enforce them.

For `claude_code`, `codex` and the two `acp:` engines, with the program
installed and no sign-in, `skep doctor` says `NO AUTH ENV` and names the
variables. The full walk-through is
[`guide-unattended-run.md`](guide-unattended-run.md).

## 5. Optional: GitHub

Runs land on local branches and need no GitHub access. You need the GitHub
CLI only for pull requests.

```sh
sudo apt install gh     # Fedora: sudo dnf install gh; macOS: brew install gh
gh auth login
gh auth setup-git       # lets `git push` use the same login over HTTPS
gh auth status
```

In every case the repository's `origin` remote must be a `github.com`
repository. Then, by task:

- **Opening a pull request.** `skep review <task_id> --approve --pr` lands a
  run and opens its PR; `skep pr open <repo> --branch <branch>` opens one for
  a local branch that already exists. Your git credentials must be able to
  push a branch to `origin`: skep pushes it with `git push`, then calls
  `gh pr create`. The PR's base branch must exist on GitHub before you
  approve. Both commands use `main` as the base unless you pass another
  (`--pr-base <base>` on `skep review`, `--base <base>` on `skep pr open`).
  If the base exists locally and is missing on GitHub, as in a repository
  that was created empty, create it first with
  `skep repo push-baseline <repo> --base <base>` (or the `push_baseline` card
  in chat). That command checks whether the base branch exists on `origin`
  and refuses to push when it finds it. If an approval
  already landed the run before the base existed, open the PR afterwards
  with `skep pr open <repo> --branch <landed-branch> --base <base>`.
- **Reviewing a pull request at its exact head.** skep reads the PR through
  `gh` and fetches its head and base from `origin` with git, so both need
  read access. The project also needs a pinned verify command (section 3):
  without one the review is never confirmed.
- **Merging through skep.** This needs a clean, confirmed review of the PR's
  current head and base, your approval, and permission to merge on GitHub.

Without `gh`, `skep review <task_id> --approve --pr` still lands the run
locally and prints
`note: gh CLI not found — push the branch and open the PR manually`.

## 6. Optional: everything else

| Feature | What it needs |
|---|---|
| The assistant's `search_files` tool | ripgrep: `sudo apt install ripgrep` (Fedora: `sudo dnf install ripgrep`; macOS: `brew install ripgrep`). Without it the tool answers `ripgrep (rg) is not installed on the host`. |
| Browser automation | `npx` on the daemon's `PATH`, and a Node.js version the Playwright MCP server accepts. Ubuntu 24.04's packaged Node 18 may be too old for the current one; check that server's own requirements. See [`browser.md`](browser.md). |
| Office and PDF documents | the `documents` extra: `pipx install --force "skep[documents]"` |
| OCR | the `ocr` extra, and the `tesseract` system program (`sudo apt install tesseract-ocr`, `brew install tesseract`) |
| Schedules that fire on time | `skep serve` kept running on a machine that stays on. A service manager keeps it running across logouts and restarts. See [`guide-ubuntu-server.md`](guide-ubuntu-server.md). |
| Approving from your phone | a messenger channel (Telegram, Discord or Slack) with confirming turned on; it is off by default. See [`mobile.md`](mobile.md) and [`daily-driver-setup.md`](daily-driver-setup.md). |

## 7. The service's `PATH` is not your shell's `PATH`

skep finds the programs it calls by name, such as `git`, `gh`, `bwrap` and an
agent's CLI, on the `PATH` of the process that runs it. In a terminal that
is your shell's `PATH`. Under systemd it is whatever the unit sets, and a
tool you installed into `~/.local/bin` is invisible unless the unit names
that directory:

```ini
[Service]
Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin
```

If a program works in your shell and a run says it is missing, check this
line first. After you change it, run `systemctl --user daemon-reload` and
restart the service.

Install the tools a worker needs outside skep's own virtual environment:
skep removes that environment from the worker's `PATH`.

## Install everything on Ubuntu 24.04

This is the set of host packages for a machine that will run sandboxed coding
runs and open pull requests. Leave out the lines for features you do not
want. To use Claude Code or Codex, also follow section 4.

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
run `brew install pipx git ripgrep gh node uv`; there is no sandbox package
to install. For a Makefile project, check that `make --version` answers, and
install Apple's Command Line Tools (`xcode-select --install`) if it does not.

## Check it

```sh
skep doctor
```

| The line says | It means | Do this |
|---|---|---|
| `sandbox: ready` | the native sandbox probe passed | nothing |
| a sandbox line with `bwrap was not found on PATH` | `bwrap` is not on skep's `PATH`; usually bubblewrap is not installed | section 2, or section 7 if it is installed |
| any other sandbox line that is not `ready` | the probe did not pass; the line gives the reason | read the reason; on Ubuntu 24.04 it is usually the AppArmor step in section 2 |
| `engine <name>: MISSING` | that agent's program is not on the `PATH` | section 4, or ignore it for an engine you do not use |
| `engine <name>: NO AUTH ENV` | the program is there, its credential is not | section 4 |
| a project under **Projects needing upgrade** | it runs unsandboxed, pins no verify command, or reads the whole disk | run the `skep project setup …` line doctor prints, with the checkout's current path. If it ends in `'<your test command>'`, install the runner (section 3) and put your command there. If doctor has no checkout path to print, it shows the flags to use once you have one. |
| `provider: ready` | a live provider answered a real completion (or the `mock` provider is configured) | nothing |

`skep onboard --check` prints the same report plus the onboarding checks. It
exits 0 when doctor's overall status and every onboarding check are ready;
doctor's advisory lines can still be there.
