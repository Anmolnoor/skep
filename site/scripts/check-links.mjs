#!/usr/bin/env node
// Link check over dist/ (run after `npm run build`, from site/).
//   1. every relative or root-absolute href/src in every page resolves to a file,
//      and a #hash on an internal link names an id on the target page;
//   2. every page of the old site still exists under the same file name;
//   3. every old `doc.html?d=<slug>` resolves to a built page;
//   4. raw markdown and assets are served, and no page links GitHub Discussions;
//   5. the footer prints the version and contract read from the Python sources.
import { existsSync, readFileSync, readdirSync, statSync } from 'node:fs';
import { dirname, join, relative, resolve } from 'node:path';
import { OLD_DOC_SLUGS, oldSlugUrl } from '../src/lib/slugs.mjs';
import { siteFacts } from '../src/lib/repo.mjs';

const DIST = resolve('dist');
// The pages of the hand-written site this one replaces (docs/*.html at 81017ffc).
// Frozen here so the check survives the PR that deletes those files.
const OLD_PAGES = [
  'agents.html', 'blog.html', 'changelog.html', 'cli.html', 'code-of-conduct.html',
  'doc.html', 'docs.html', 'first-run.html', 'how-it-works.html', 'index.html',
  'install.html', 'open-source.html', 'privacy.html', 'roadmap.html', 'security.html',
];

if (!existsSync(join(DIST, 'index.html'))) {
  console.error('check-links: dist/index.html not found. Run `npm run build` in site/ first.');
  process.exit(2);
}

const walk = (dir) =>
  readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    return statSync(path).isDirectory() ? walk(path) : [path];
  });

const problems = [];
const pages = walk(DIST).filter((path) => path.endsWith('.html') && !path.includes('/pagefind/'));
const ids = new Map();
const idsOf = (file) => {
  if (!ids.has(file)) {
    ids.set(file, new Set([...readFileSync(file, 'utf8').matchAll(/\sid="([^"]+)"/g)].map((m) => m[1])));
  }
  return ids.get(file);
};
const decode = (text) => text.replace(/&amp;/g, '&').replace(/&#38;/g, '&').replace(/&quot;/g, '"');

let checked = 0;
for (const page of pages) {
  const html = readFileSync(page, 'utf8');
  const where = relative(DIST, page);
  for (const [, attr, raw] of html.matchAll(/\s(href|src)="([^"]*)"/g)) {
    const link = decode(raw);
    if (/github\.com\/[^/]+\/[^/]+\/discussions/i.test(link)) {
      problems.push(`${where}: links GitHub Discussions (disabled on the repo): ${link}`);
    }
    if (!link || /^([a-z][a-z0-9+.-]*:|\/\/)/i.test(link)) continue; // external, mailto:, data:
    checked += 1;
    const [pathAndQuery, hash = ''] = link.split('#');
    const path = pathAndQuery.split('?')[0];
    let target = page;
    if (path) {
      target = path.startsWith('/') ? join(DIST, path) : resolve(dirname(page), path);
      if (target === DIST || path.endsWith('/')) target = join(target, 'index.html');
      if (!existsSync(target) || statSync(target).isDirectory()) {
        problems.push(`${where}: ${attr}="${link}" does not resolve`);
        continue;
      }
    }
    if (hash && target.endsWith('.html') && !idsOf(target).has(decodeURIComponent(hash))) {
      problems.push(`${where}: ${attr}="${link}" has no element with id "${hash}"`);
    }
  }
}

const oldDocs = resolve('..', 'docs');
const oldPages = new Set(OLD_PAGES);
if (existsSync(oldDocs)) {
  for (const name of readdirSync(oldDocs)) if (name.endsWith('.html')) oldPages.add(name);
}
for (const name of oldPages) {
  if (!existsSync(join(DIST, name))) problems.push(`old URL /${name} is missing from dist/`);
}

for (const [slug, file] of Object.entries(OLD_DOC_SLUGS)) {
  if (!existsSync(join(DIST, oldSlugUrl(slug)))) {
    problems.push(`doc.html?d=${slug} -> ${oldSlugUrl(slug)} is missing from dist/`);
  }
  if (!existsSync(join(DIST, file))) problems.push(`raw markdown /${file} is missing from dist/`);
}
for (const served of ['README.md', 'adr/README.md', 'assets/skep-cover.png', 'favicon.svg', 'pagefind/pagefind.js']) {
  if (!existsSync(join(DIST, served))) problems.push(`/${served} is missing from dist/`);
}

const { version, contract } = siteFacts();
const home = readFileSync(join(DIST, 'index.html'), 'utf8');
if (!home.includes(`v${version}`)) problems.push(`index.html does not print v${version} from pyproject.toml`);
if (!home.includes(`worker contract ${contract}`)) {
  problems.push(`index.html does not print worker contract ${contract} from worker_contract/version.py`);
}

if (problems.length > 0) {
  console.error(`check-links: ${problems.length} problem(s)\n` + problems.join('\n'));
  process.exit(1);
}
console.log(
  `check-links: ok (${pages.length} pages, ${checked} internal links, ` +
    `${oldPages.size} old URLs, ${Object.keys(OLD_DOC_SLUGS).length} old doc slugs)`,
);
