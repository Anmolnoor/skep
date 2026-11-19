// Reads the facts the site prints from the repository around it. Not pure (it
// touches the filesystem), so it has no node test; the parsers it calls do.
import { existsSync, readFileSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { contractVersion, projectVersion } from './facts.mjs';

// Astro bundles this module, so import.meta.url is not where the file lives.
// Every site command (dev, build, the scripts) runs with site/ as the working directory.
export const REPO_ROOT = resolve(process.cwd(), '..');
export const DOCS_DIR = join(REPO_ROOT, 'docs');

export function readRepoFile(relPath) {
  const path = join(REPO_ROOT, relPath);
  if (!existsSync(path)) {
    throw new Error(`${relPath} not found from ${process.cwd()}: run site commands inside site/`);
  }
  return readFileSync(path, 'utf8');
}

let facts;

/** `{ version, contract }`, read once per build. */
export function siteFacts() {
  facts ??= {
    version: projectVersion(readRepoFile('pyproject.toml')),
    contract: contractVersion(readRepoFile('src/skep/worker_contract/version.py')),
  };
  return facts;
}
