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

// ---- The numbers strip on the home page: counted from the sources at build, never typed. ----
// Each reader throws when its source no longer has the shape it was written
// against, so a refactor fails the build instead of printing a wrong number.

/** Engine ids: the keys of `CODING_ENGINES` in src/skep/supervisor/engines.py, in source order. */
export function engineNames(source) {
  const block = /^CODING_ENGINES\b[^\n]*=\s*\{\n([\s\S]*?)^\}/m.exec(source);
  if (!block) throw new Error('engines.py: no CODING_ENGINES dict');
  const builtin = /^BUILTIN_ENGINE\s*=\s*"([^"]+)"/m.exec(source);
  const names = [...block[1].matchAll(/^ {4}(?:"([^"]+)"|(BUILTIN_ENGINE)): CodingEngine\(/gm)].map(
    ([, literal, constant]) => {
      if (constant && !builtin) throw new Error('engines.py: CODING_ENGINES uses BUILTIN_ENGINE, which is not defined');
      return literal ?? builtin[1];
    },
  );
  // every `CodingEngine(` in the dict must have been read as an entry
  const constructed = block[1].match(/\bCodingEngine\(/g)?.length ?? 0;
  if (names.length === 0 || names.length !== constructed || new Set(names).size !== names.length) {
    throw new Error(`engines.py: read ${names.length} engine ids from ${constructed} CodingEngine entries`);
  }
  return names;
}

/** The outcomes a re-verification can record: the first argument of every `ReverifyOutcome(` in reverify.py. */
export function reverifyOutcomes(source) {
  const calls = source.match(/\bReverifyOutcome\(\s*(?!\s*\))/g)?.length ?? 0;
  const literals = [...source.matchAll(/\bReverifyOutcome\(\s*"([a-z_]+)"/g)].map((match) => match[1]);
  if (literals.length === 0 || literals.length !== calls) {
    throw new Error(`reverify.py: ${calls} ReverifyOutcome(...) calls, ${literals.length} with a literal outcome`);
  }
  const outcomes = [...new Set(literals)];
  for (const expected of ['passed', 'failed']) {
    if (!outcomes.includes(expected)) throw new Error(`reverify.py: no ReverifyOutcome("${expected}", ...)`);
  }
  return outcomes;
}

/** Sandbox backends: every backend sandbox.py can report as available (`SandboxAvailability(True, backend=…)`). */
export function sandboxBackends(source) {
  const backends = [
    ...new Set(
      [...source.matchAll(/\bSandboxAvailability\(\s*True,\s*backend=("[a-z_]+"|[A-Z_]+)\s*\)/g)].map((match) => {
        if (match[1].startsWith('"')) return match[1].slice(1, -1);
        const constant = new RegExp(`^${match[1]}\\s*=\\s*"([^"]+)"`, 'm').exec(source);
        if (!constant) throw new Error(`sandbox.py: backend constant ${match[1]} is not defined`);
        return constant[1];
      }),
    ),
  ];
  if (backends.length === 0) throw new Error('sandbox.py: no SandboxAvailability(True, backend=...)');
  return backends;
}

/** Design invariants: the `**I<n>. Title**` paragraphs of docs/invariants.md, which must number 1..N. */
export function invariantCount(markdown) {
  const numbers = [...markdown.matchAll(/^\*\*I(\d+)\. /gm)].map((match) => Number(match[1]));
  if (numbers.length === 0 || numbers.some((number, index) => number !== index + 1)) {
    throw new Error(`invariants.md: expected I1..IN in order, read [${numbers.join(', ')}]`);
  }
  return numbers.length;
}

/** Architecture decision records: the `NNNN-title.md` files of docs/adr/ (pass the directory listing). */
export function adrCount(fileNames) {
  const records = fileNames.filter((name) => /^\d{4}-[^/]+\.md$/.test(name));
  if (records.length === 0) throw new Error('docs/adr: no NNNN-title.md files');
  if (new Set(records.map((name) => name.slice(0, 4))).size !== records.length) {
    throw new Error('docs/adr: two records share a number');
  }
  return records.length;
}

/**
 * The home page's numbers, read once per build. `read(relPath)` returns a
 * repository file and `list(relDir)` a directory listing (index.astro passes
 * `readRepoFile` and `readdirSync`), which keeps this module free of the filesystem.
 */
export function homeNumbers({ read, list }) {
  return {
    engines: engineNames(read('src/skep/supervisor/engines.py')),
    outcomes: reverifyOutcomes(read('src/skep/supervisor/reverify.py')),
    backends: sandboxBackends(read('src/skep/supervisor/sandbox.py')),
    invariants: invariantCount(read('docs/invariants.md')),
    adrs: adrCount(list('docs/adr')),
  };
}
