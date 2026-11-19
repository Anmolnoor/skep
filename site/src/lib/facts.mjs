// Version and worker-contract strings are read from the Python sources at build,
// never typed into a page (plan §3.2). Pure parsers: no npm imports.

/** `version = "…"` from the `[project]` table of pyproject.toml. */
export function projectVersion(pyproject) {
  const project = /^\[project\]\s*$([\s\S]*?)(?=^\[|(?![\s\S]))/m.exec(pyproject);
  const match = project && /^version\s*=\s*"([^"]+)"/m.exec(project[1]);
  if (!match) throw new Error('pyproject.toml: no [project] version');
  return match[1];
}

/** `CONTRACT_VERSION = "…"` from src/skep/worker_contract/version.py. */
export function contractVersion(source) {
  const match = /^CONTRACT_VERSION\s*=\s*"([^"]+)"/m.exec(source);
  if (!match) throw new Error('worker_contract/version.py: no CONTRACT_VERSION');
  return match[1];
}
