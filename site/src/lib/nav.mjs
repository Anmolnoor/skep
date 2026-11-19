// The docs sidebar and hub come from docs/README.md (plan §3.2). Pure: no npm imports.
import { rewriteHref } from './links.mjs';
import { docId } from './slugs.mjs';

const ITEM = /^- \[`?([^`\]]+)`?\]\(([^)\s]+)\)\s*(?:—\s*(.*))?$/;

/**
 * Parse the curated index into `[{ title, items: [{ file, href, id, description }] }]`.
 * `id` is the docs-collection id for a doc under docs/, or null for a link that
 * leaves it (`../CONTRIBUTING.md`); `href` is always a usable URL.
 */
export function parseDocsIndex(readme) {
  const sections = [];
  let pending = null;
  const flush = () => {
    if (pending === null) return;
    const match = ITEM.exec(pending.replace(/\s+/g, ' ').trim());
    pending = null;
    if (!match || sections.length === 0) return;
    const [, , target, description = ''] = match;
    const href = rewriteHref(target, 'README.md');
    const internal = href.startsWith('/docs/');
    sections.at(-1).items.push({
      file: target,
      href,
      id: internal ? docId(target) : null,
      description: description.trim(),
    });
  };
  for (const line of readme.split('\n')) {
    if (line.startsWith('## ')) {
      flush();
      sections.push({ title: line.slice(3).trim(), items: [] });
    } else if (line.startsWith('- ')) {
      flush();
      pending = line;
    } else if (pending !== null && /^\s+\S/.test(line)) {
      pending += ` ${line.trim()}`;
    } else {
      flush();
    }
  }
  flush();
  return sections.filter((section) => section.items.length > 0);
}

/** First `# ` heading of a markdown file, as plain text. */
export function docTitle(body, fallback = '') {
  const match = /^# +(.+)$/m.exec(body ?? '');
  return match ? inlineText(match[1]) : fallback;
}

/** Inline markdown -> plain text: links keep their label, code keeps its text. */
export function inlineText(markdown) {
  return markdown
    .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
    .replace(/[`*]/g, '')
    .trim();
}

const escapeHtml = (text) =>
  text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

/** Inline markdown -> safe HTML for a one-line description: only <code> survives. */
export function inlineHtml(markdown) {
  return escapeHtml(markdown.replace(/\[([^\]]+)\]\([^)]*\)/g, '$1').replace(/\*/g, '')).replace(
    /`([^`]+)`/g,
    '<code>$1</code>',
  );
}

/** Previous and next entry around `id` in an ordered id list. */
export function neighbours(ids, id) {
  const index = ids.indexOf(id);
  if (index === -1) return { prev: null, next: null };
  return { prev: ids[index - 1] ?? null, next: ids[index + 1] ?? null };
}
