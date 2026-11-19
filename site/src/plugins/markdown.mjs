// The Sätteri wrapper around src/lib/links.mjs. This file may import npm packages
// and is not run under `node --test`; the rewriting rules themselves are in the lib.
import { relative, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createSatteriMarkdownProcessor } from '@astrojs/markdown-satteri';
import { rewriteHref } from '../lib/links.mjs';

/** mdast plugin: rewrite relative links, reference definitions and images in docs/*.md. */
export function rewriteLinks({ docsDir }) {
  const fix = (node, ctx) => {
    if (!ctx.fileURL || !node.url) return;
    const from = relative(docsDir, fileURLToPath(ctx.fileURL)).split(sep).join('/');
    if (from.startsWith('..')) return;
    const next = rewriteHref(node.url, from);
    if (next !== node.url) ctx.setField(node, 'url', next);
  };
  return { name: 'skep-rewrite-links', link: fix, definition: fix, image: fix };
}

let processor;

/**
 * Render a markdown fragment that came out of `docs/<fromFile>` (a changelog
 * section) with the same link rewriting and highlighting as a whole doc page.
 */
export async function renderDocsFragment(markdown, fromFile, docsDir) {
  processor ??= createSatteriMarkdownProcessor({
    mdastPlugins: [rewriteLinks({ docsDir })],
    features: { smartPunctuation: false },
    shikiConfig: { theme: 'github-dark-default' },
  });
  const { code } = await (await processor).render(markdown, {
    fileURL: new URL(fromFile, `file://${docsDir.endsWith('/') ? docsDir : `${docsDir}/`}`),
  });
  return code;
}
