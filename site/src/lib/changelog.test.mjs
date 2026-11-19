import assert from 'node:assert/strict';
import { test } from 'node:test';
import { compareVersions, parseReleases, publishedReleases } from './changelog.mjs';

// Fixture versions are made up on purpose: no real skep version is typed under site/src.
const HISTORY = `# history

## v77 (2099-01-01, F1–F5)

internal round, not a release

## v7.1.0 — the first one (2099-02-01)

Body of 7.1.0.

## some-track B0 — not a release (2099-02-10)

track notes

## v7.1.1 — a patch (2099-03-01, never published; shipped in v7.2.1)

Never published.

- a bullet

## v7.2.0 — a minor (2099-03-05, never published; shipped in v7.2.1)

minor body

## v7.2.1 — the carrier (2099-03-09)

carrier body

## v7.10.0 — double digits (2099-04-01)

latest body
`;

test('only `## vX.Y.Z — title (date)` headings are releases', () => {
  const releases = parseReleases(HISTORY);
  assert.deepEqual(
    releases.map((release) => release.version),
    ['7.1.0', '7.1.1', '7.2.0', '7.2.1', '7.10.0'],
  );
  assert.equal(releases[0].title, 'the first one');
  assert.equal(releases[0].date, '2099-02-01');
  assert.equal(releases[0].body, 'Body of 7.1.0.');
});

test('a section body stops at the next heading of any kind', () => {
  const [first] = parseReleases(HISTORY);
  assert.ok(!first.body.includes('track notes'));
  assert.equal(parseReleases(HISTORY)[1].body, 'Never published.\n\n- a bullet');
});

test('unpublished versions are folded into the release that shipped them', () => {
  const published = publishedReleases(HISTORY);
  assert.deepEqual(
    published.map((release) => release.version),
    ['7.10.0', '7.2.1', '7.1.0'],
  );
  const carrier = published[1];
  assert.deepEqual(
    carrier.includes.map((release) => release.version),
    ['7.2.0', '7.1.1'],
  );
  assert.deepEqual(published[0].includes, []);
});

test('versions sort numerically, not as text', () => {
  assert.ok(compareVersions('7.10.0', '7.9.9') > 0);
  assert.equal(compareVersions('7.2.1', '7.2.1'), 0);
});

test('a version folded into a release with no section fails the build', () => {
  const broken = '## v7.0.1 — orphan (2099-01-01, never published; shipped in v7.0.2)\n';
  assert.throws(() => publishedReleases(broken), /shipped in v7\.0\.2/);
});
