#!/usr/bin/env bash
# Release gate: fail on automatable source/history hygiene issues before launch.
# The broader key/token/password history grep remains a human review item in
# docs/release-checklist.md because test fixtures and auth code are noisy there.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

scan_paths=(
  README.md
  docs
  examples
  CONTRIBUTING.md
  Makefile
  pyproject.toml
  src/skep
  tests
  .github
  .gitignore
  Dockerfile
  Dockerfile.dockerignore
  docker-compose.yml
  SECURITY.md
  agent-task-contract-spec-v0.1.md
)

# Extra arguments after the pattern are passed to rg (e.g. -g '!path'
# exclusions for hits that are deliberate).
scan_pattern() {
  local label="$1"
  local pattern="$2"
  shift 2
  local output
  local status

  output="$(mktemp "${TMPDIR:-/tmp}/skep-hygiene.XXXXXX")"
  set +e
  rg -n "$pattern" "$@" "${scan_paths[@]}" > "$output"
  status=$?
  set -e
  if [[ "$status" -eq 0 ]]; then
    echo "release hygiene failed: $label" >&2
    cat "$output" >&2
    rm -f "$output"
    exit 1
  fi
  rm -f "$output"
  if [[ "$status" -gt 1 ]]; then
    echo "release hygiene scan errored: $label" >&2
    exit "$status"
  fi
  echo "$label: ok"
}

old_names='\bfcli\b|\bBeekeeper\b|\bbeekeeper\b|foundation run'
# Absolute paths only, on purpose. Bare "anmolnoor" is the project's own
# namespace — github.com/Anmolnoor/skep, ghcr.io/anmolnoor/skep,
# skep.anmolnoor.com — and tightening this pattern to match it produces 18
# false positives that teach the operator to ignore the scanner.
personal_paths='/Users/anmolnoor|/home/anmolnoor'
personal_emails='@(gmail|proton|outlook)\.'
secret_patterns='sk-[A-Za-z0-9_-]{20,}|ghp_[A-Za-z0-9_]{30,}|github_pat_[A-Za-z0-9_]+|AKIA[0-9A-Z]{16}|-----BEGIN (RSA |OPENSSH |EC |DSA |PRIVATE )?PRIVATE KEY-----|api[_-]?key\s*=\s*['"'"'"][^'"'"'"]{16,}['"'"'"]|secret\s*=\s*['"'"'"][^'"'"'"]{16,}['"'"'"]|password\s*=\s*['"'"'"][^'"'"'"]{16,}['"'"'"]|token\s*=\s*['"'"'"][^'"'"'"]{24,}['"'"'"]'

scan_pattern "old project names" "$old_names"
scan_pattern "personal machine paths" "$personal_paths"
# The excluded files carry a published contact address on purpose (security
# and conduct reporting need a private channel; launch.md publishes the
# consulting contact); a scanner that flags them teaches the operator to
# ignore the scanner. SECURITY.md is dropped from the path list
# rather than globbed out — rg -g globs filter traversal, not explicitly
# named files.
all_scan_paths=("${scan_paths[@]}")
scan_paths=()
for scan_path in "${all_scan_paths[@]}"; do
  [[ "$scan_path" == "SECURITY.md" ]] || scan_paths+=("$scan_path")
done
scan_pattern "personal email addresses" "$personal_emails" \
  -g '!docs/security.html' -g '!docs/code-of-conduct.html' -g '!docs/launch.md'
scan_paths=("${all_scan_paths[@]}")

set +e
secret_output="$(mktemp "${TMPDIR:-/tmp}/skep-secret-history.XXXXXX")"
git log --all -p | rg -n -i "$secret_patterns" > "$secret_output"
secret_status=$?
set -e
if [[ "$secret_status" -eq 0 ]]; then
  echo "release hygiene failed: narrow secret patterns" >&2
  cat "$secret_output" >&2
  rm -f "$secret_output"
  exit 1
fi
rm -f "$secret_output"
if [[ "$secret_status" -gt 1 ]]; then
  echo "release hygiene scan errored: narrow secret patterns" >&2
  exit "$secret_status"
fi
echo "narrow secret patterns: ok"

# Working-tree secret scan over the paths that ship (not `.` — the private
# checkout also holds plans/ and untracked local files that never leave it;
# history never leaves either, so there is no history to scan). Skipping when
# the binary is absent is honest for a laptop run — CI installs gitleaks, so
# the gate is real where it counts.
if command -v gitleaks >/dev/null 2>&1; then
  gitleaks_failed=0
  for scan_path in "${scan_paths[@]}"; do
    if ! gitleaks detect --no-banner --no-git --redact --source "$scan_path"; then
      gitleaks_failed=1
    fi
  done
  if [[ "$gitleaks_failed" -ne 0 ]]; then
    echo "release hygiene failed: gitleaks" >&2
    exit 1
  fi
  echo "gitleaks: ok"
else
  echo "gitleaks: skipped (not installed)"
fi

echo "RELEASE HYGIENE PASS"
