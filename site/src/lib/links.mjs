// Markdown link rewriting (plan §3.2). Pure: node builtins only, no npm imports.
import { posix } from 'node:path';
import { docId, docUrl, isMaintainerDoc } from './slugs.mjs';

export const GH_REPO = 'Anmolnoor/skep';
export const GH_URL = `https://github.com/${GH_REPO}`;
const GH_BLOB = `${GH_URL}/blob/main/`;
const GH_TREE = `${GH_URL}/tree/main/`;

/**
 * Rewrite one href found in `docs/<fromFile>`.
 *   x.md, adr/0050-foo.md   -> /docs/x.html, /docs/adr/0050-foo.html
 *   assets/cover.png        -> /assets/cover.png (the build copies docs/assets)
 *   ../SKILL.md, adr/       -> the GitHub blob / tree URL
 *   launch.md, releases/…   -> the GitHub blob URL (maintainer records are not site pages)
 * Absolute URLs, `#hash` links and root-absolute paths pass through.
 */
export function rewriteHref(href, fromFile) {
  if (!href || /^([a-z][a-z0-9+.-]*:|#|\/)/i.test(href)) return href;
  const cut = href.search(/[?#]/);
  const target = cut === -1 ? href : href.slice(0, cut);
  const tail = cut === -1 ? '' : href.slice(cut);
  const hash = tail.includes('#') ? tail.slice(tail.indexOf('#')) : '';
  if (!target) return href;
  const resolved = posix.normalize(posix.join('docs', posix.dirname(fromFile), target));
  if (resolved.startsWith('..')) return href; // leaves the repository: not ours to rewrite
  if (resolved.startsWith('docs/')) {
    const rel = resolved.slice('docs/'.length);
    if (/\.md$/i.test(rel) && !isMaintainerDoc(rel)) return docUrl(docId(rel)) + hash;
    if (rel.startsWith('assets/')) return `/${rel}`;
  }
  return (resolved.endsWith('/') ? GH_TREE : GH_BLOB) + resolved + hash;
}

/** "Edit on GitHub" URL for `docs/<relPath>`. */
export function editUrl(relPath) {
  return `${GH_URL}/edit/main/docs/${relPath}`;
}
