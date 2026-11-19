import assert from 'node:assert/strict';
import { test } from 'node:test';
import { OLD_DOC_SLUGS, docId, docUrl, oldSlugTable, oldSlugUrl } from './slugs.mjs';

test('doc ids are the lowercased path without the extension', () => {
  assert.equal(docId('quickstart.md'), 'quickstart');
  assert.equal(docId('README.md'), 'readme');
  assert.equal(docId('adr/README.md'), 'adr/readme');
  assert.equal(docId('adr/0050-operator-face.md'), 'adr/0050-operator-face');
  assert.equal(docUrl('adr/readme'), '/docs/adr/readme.html');
});

test('the old doc.html slugs that are not file names still resolve', () => {
  assert.equal(Object.keys(OLD_DOC_SLUGS).length, 15);
  assert.equal(oldSlugUrl('cli'), '/docs/cli-reference.html');
  assert.equal(oldSlugUrl('new-machine'), '/docs/how-to-use-on-new-machine.html');
  assert.equal(oldSlugUrl('quickstart'), '/docs/quickstart.html');
});

test('an unknown slug lands on the hub, not on a 404', () => {
  assert.equal(oldSlugUrl('nope'), '/docs.html');
  assert.equal(oldSlugUrl('__proto__'), '/docs.html');
});

test('the shim table covers every old slug', () => {
  const table = oldSlugTable();
  assert.deepEqual(Object.keys(table), Object.keys(OLD_DOC_SLUGS));
  assert.equal(table['version-history'], '/docs/version-history.html');
});
