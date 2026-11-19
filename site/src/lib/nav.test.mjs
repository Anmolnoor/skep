import assert from 'node:assert/strict';
import { test } from 'node:test';
import { docTitle, inlineHtml, inlineText, neighbours, parseDocsIndex } from './nav.mjs';

const README = `# skep documentation

The curated index.

## Start here

- [\`quickstart.md\`](quickstart.md) — first run in 5 minutes
- [\`onboarding.md\`](onboarding.md) — \`skep onboard\`: model, work directory,
  sandbox, locked policy baseline, role — and \`--check\`
- [\`agent-guide.md\`](agent-guide.md) — for agents (the operating half is
  [\`../SKILL.md\`](../SKILL.md))

## Decisions

- [\`adr/README.md\`](adr/README.md) — the architecture decision record index

## Contributing

- [\`../CONTRIBUTING.md\`](../CONTRIBUTING.md) — development setup

## Empty
`;

test('sections and items come out in file order', () => {
  const sections = parseDocsIndex(README);
  assert.deepEqual(
    sections.map((section) => section.title),
    ['Start here', 'Decisions', 'Contributing'],
  );
  assert.deepEqual(
    sections[0].items.map((item) => item.id),
    ['quickstart', 'onboarding', 'agent-guide'],
  );
});

test('a wrapped description is joined, and keeps its own dashes', () => {
  const [start] = parseDocsIndex(README);
  assert.equal(
    start.items[1].description,
    '`skep onboard`: model, work directory, sandbox, locked policy baseline, role — and `--check`',
  );
  assert.equal(start.items[0].href, '/docs/quickstart.html');
});

test('a doc in a subdirectory and a link that leaves docs/', () => {
  const sections = parseDocsIndex(README);
  assert.equal(sections[1].items[0].id, 'adr/readme');
  assert.equal(sections[1].items[0].href, '/docs/adr/readme.html');
  const [contributing] = sections[2].items;
  assert.equal(contributing.id, null);
  assert.equal(contributing.href, 'https://github.com/Anmolnoor/skep/blob/main/CONTRIBUTING.md');
});

test('the title is the first h1, as plain text', () => {
  assert.equal(docTitle('# `skep onboard` — first run\n\ntext'), 'skep onboard — first run');
  assert.equal(docTitle('intro\n\n# Real *title*\n## not this'), 'Real title');
  assert.equal(docTitle('no heading', 'fallback'), 'fallback');
});

test('inline markdown renders safely', () => {
  assert.equal(inlineText('see [`../SKILL.md`](../SKILL.md) and `x`'), 'see ../SKILL.md and x');
  assert.equal(inlineHtml('`a<b>` & [t](u)'), '<code>a&lt;b&gt;</code> &amp; t');
});

test('previous and next stop at the ends', () => {
  const ids = ['a', 'b', 'c'];
  assert.deepEqual(neighbours(ids, 'a'), { prev: null, next: 'b' });
  assert.deepEqual(neighbours(ids, 'c'), { prev: 'b', next: null });
  assert.deepEqual(neighbours(ids, 'zzz'), { prev: null, next: null });
});
