import assert from 'node:assert/strict';
import { test } from 'node:test';
import { editUrl, rewriteHref } from './links.mjs';

const BLOB = 'https://github.com/Anmolnoor/skep/blob/main/';

test('a sibling doc becomes its page', () => {
  assert.equal(rewriteHref('quickstart.md', 'README.md'), '/docs/quickstart.html');
  assert.equal(rewriteHref('./sandboxing.md', 'how-it-works.md'), '/docs/sandboxing.html');
});

test('the hash survives, a query does not reach the page URL', () => {
  assert.equal(rewriteHref('approvals.md#templates', 'README.md'), '/docs/approvals.html#templates');
  assert.equal(rewriteHref('approvals.md?x=1#t', 'README.md'), '/docs/approvals.html#t');
});

test('subdirectories resolve in both directions', () => {
  assert.equal(rewriteHref('adr/README.md', 'README.md'), '/docs/adr/readme.html');
  assert.equal(rewriteHref('adr/0050-Operator-Face.md', 'invariants.md'), '/docs/adr/0050-operator-face.html');
  assert.equal(rewriteHref('../invariants.md', 'adr/0050-x.md'), '/docs/invariants.html');
  assert.equal(rewriteHref('0049-y.md', 'adr/0050-x.md'), '/docs/adr/0049-y.html');
});

test('anything that leaves docs/ goes to GitHub', () => {
  assert.equal(rewriteHref('../SKILL.md', 'README.md'), `${BLOB}SKILL.md`);
  assert.equal(rewriteHref('../CONTRIBUTING.md#gates', 'README.md'), `${BLOB}CONTRIBUTING.md#gates`);
  assert.equal(
    rewriteHref('../examples/skep-demo/README.md', 'demo-gif.md'),
    `${BLOB}examples/skep-demo/README.md`,
  );
  assert.equal(rewriteHref('../../src/skep/cli.py', 'adr/0001-x.md'), `${BLOB}src/skep/cli.py`);
});

test('a directory link is a GitHub tree, an asset is served from the site root', () => {
  assert.equal(rewriteHref('adr/', 'README.md'), 'https://github.com/Anmolnoor/skep/tree/main/docs/adr/');
  assert.equal(rewriteHref('assets/skep-cover.png', 'README.md'), '/assets/skep-cover.png');
});

test('absolute URLs, hashes and root paths pass through', () => {
  for (const href of ['https://example.com/a.md', 'mailto:a@example.com', '#top', '/docs.html', '']) {
    assert.equal(rewriteHref(href, 'README.md'), href);
  }
});

test('a link above the repository root is left alone', () => {
  assert.equal(rewriteHref('../../elsewhere.md', 'README.md'), '../../elsewhere.md');
});

test('edit links point at the markdown source', () => {
  assert.equal(
    editUrl('adr/README.md'),
    'https://github.com/Anmolnoor/skep/edit/main/docs/adr/README.md',
  );
});
