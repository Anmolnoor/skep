# Skep Field Test — Task List

Date: 2026-07-15
Tester: Hermes Agent (automated)
Skep version: 1.0.1 (worker contract 0.3.1)
Provider: Ollama (ollama.com) model gml-5.2
Sandbox: bubblewrap (Linux)

## Goal

Exercise skep end-to-end as a day-to-day user would: through the CLI chat
(`skep chat --oneshot`), the HTTP API, and direct CLI subcommands. Test every
surface a daily user touches, observe what works, what breaks, and what is off.

## Tasks

### 1. Basic chat — Queen responds
- Send a oneshot message to a new chat
- Verify the Queen streams a reply
- Confirm the chat is persisted

### 2. Command deck — read commands
- `/help` — list the deck
- `/repos` — registered repos
- `/runs` — recent runs
- `/approvals` — pending approval queue
- `/policy` — effective policy

### 3. Command deck — /workon on a local dir
- Create a throwaway test project (git repo with a simple Python file)
- `/workon <path>` — make it a first-class workspace
- Confirm the git baseline + trusted project setup

### 4. Run a task on the test project
- Ask the Queen to dispatch a coding task (e.g. "add a hello function")
- Watch the run events stream
- Check the run produces a patch

### 5. Approval flow
- Observe a run hitting an approval gate
- Test approve once, deny, and skip paths

### 6. Review and patch landing
- `skep review <task_id>` — inspect the patch
- `skep review <task_id> --approve` — land on a branch
- Verify the branch was created

### 7. Templates
- `skep template list` — existing templates
- `skep template add` — create a new template
- `skep template show` — inspect it
- `skep template suggest` — suggest from learned approvals

### 8. Schedules
- `skep schedule add` — create a recurring schedule
- `skep schedule list` — list schedules
- `skep schedule remove` — remove it

### 9. Policy template switching
- `skep setup --template <name> --dry-run` — preview a template
- `skep setup --template <name>` — apply it
- Verify policy changed

### 10. Personality setting
- `/personality concise` — set chat reply style
- Verify the next reply is concise

### 11. /state on a repo
- `/state <repo>` — git state: branches, HEAD, recent commits

### 12. CLI subcommands
- `skep status --personal` — recent supervised runs
- `skep doctor` — readiness check
- `skep template list`
- `skep schedule list`

### 13. Logs and audit trail
- Read the serve log
- Read the audit trail from the sqlite store
- Check for errors, warnings, anomalies

### 14. Final report
- Write a detailed report to `reports/field-test-2026-07-15.md`