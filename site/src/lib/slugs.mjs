// Doc ids and URLs. Pure: node builtins only, no npm imports (run under `node --test`).

/** `adr/0050-Foo.md` -> `adr/0050-foo`. content.config.ts uses this as the collection id. */
export function docId(relPath) {
  return relPath.replace(/\.md$/i, '').toLowerCase();
}

/** Collection id -> the page URL (`build.format: 'file'`). */
export function docUrl(id) {
  return `/docs/${id}.html`;
}

// The old site's `doc.html?d=<slug>` map (docs/doc.html:84-98 at 81017ffc).
// Old slugs are not file names, so the shim needs this table forever.
export const OLD_DOC_SLUGS = {
  quickstart: 'quickstart.md',
  'new-machine': 'how-to-use-on-new-machine.md',
  'how-it-works': 'how-it-works.md',
  sandboxing: 'sandboxing.md',
  verification: 'verification.md',
  approvals: 'approvals.md',
  workers: 'workers.md',
  cli: 'cli-reference.md',
  configuration: 'configuration.md',
  brain: 'brain.md',
  'assistant-tools': 'assistant-tools.md',
  browser: 'browser.md',
  mobile: 'mobile.md',
  invariants: 'invariants.md',
  'version-history': 'version-history.md',
};

/** Old slug -> new URL. An unknown slug goes to the hub, never to a 404. */
export function oldSlugUrl(slug) {
  // hasOwn: `?d=__proto__` must not reach Object.prototype.
  return Object.hasOwn(OLD_DOC_SLUGS, slug) ? docUrl(docId(OLD_DOC_SLUGS[slug])) : '/docs.html';
}

/** `{slug: url}` for the redirect shim's inline script. */
export function oldSlugTable() {
  return Object.fromEntries(Object.keys(OLD_DOC_SLUGS).map((slug) => [slug, oldSlugUrl(slug)]));
}
