#!/usr/bin/env node
// JS budget (plan §3.3): the home page may load at most 120 KB of JavaScript,
// gzipped. Counted: inline scripts, <script src>, modulepreloads, every island's
// component and renderer, and everything those import statically. Not counted:
// the search UI, which loads only when a reader opens search.
import { existsSync, readFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { gzipSync } from 'node:zlib';

const DIST = resolve('dist');
const BUDGET = 120 * 1024;
const page = join(DIST, 'index.html');
if (!existsSync(page)) {
  console.error('check-js-budget: dist/index.html not found. Run `npm run build` in site/ first.');
  process.exit(2);
}
const html = readFileSync(page, 'utf8');

const files = new Map(); // path -> gzip bytes
const queue = [];
const add = (url, from) => {
  if (/^([a-z][a-z0-9+.-]*:|\/\/)/i.test(url)) throw new Error(`third-party script on the home page: ${url}`);
  const path = url.startsWith('/') ? join(DIST, url.split('?')[0]) : resolve(dirname(from), url.split('?')[0]);
  if (files.has(path)) return;
  if (!existsSync(path)) throw new Error(`script ${url} (from ${from}) is not in dist/`);
  files.set(path, 0);
  queue.push(path);
};

for (const [, url] of html.matchAll(/<script\b[^>]*\ssrc="([^"]+)"/g)) add(url, page);
for (const [, url] of html.matchAll(/<link\b[^>]*rel="modulepreload"[^>]*href="([^"]+)"/g)) add(url, page);
for (const [, url] of html.matchAll(/\s(?:component-url|renderer-url|before-hydration-url)="([^"]+)"/g)) {
  add(url, page);
}
while (queue.length > 0) {
  const path = queue.pop();
  const source = readFileSync(path, 'utf8');
  files.set(path, gzipSync(source).length);
  // static imports only: `import "./x.js"`, `from "./x.js"`. A dynamic import() is on demand.
  for (const [, url] of source.matchAll(/(?:\bfrom|\bimport)\s*["'](\.{0,2}\/[^"']+)["']/g)) add(url, path);
}

const inline = [...html.matchAll(/<script\b(?![^>]*\ssrc=)[^>]*>([\s\S]*?)<\/script>/g)]
  .map((match) => match[1])
  .join('\n');
const inlineBytes = inline ? gzipSync(inline).length : 0;
const total = inlineBytes + [...files.values()].reduce((sum, bytes) => sum + bytes, 0);

const kb = (bytes) => `${(bytes / 1024).toFixed(1)} KB`;
for (const [path, bytes] of [...files].sort((a, b) => b[1] - a[1])) {
  console.log(`  ${kb(bytes).padStart(9)}  ${path.slice(DIST.length)}`);
}
console.log(`  ${kb(inlineBytes).padStart(9)}  (inline scripts)`);
if (total > BUDGET) {
  console.error(`check-js-budget: FAIL: home page JS is ${kb(total)} gzip, over the ${kb(BUDGET)} budget`);
  process.exit(1);
}
console.log(`check-js-budget: ok (home page JS ${kb(total)} gzip of ${kb(BUDGET)})`);
