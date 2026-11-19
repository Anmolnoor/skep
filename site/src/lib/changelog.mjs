// The changelog is generated from the release sections of docs/version-history.md
// (plan §3.2): headings shaped `## vX.Y.Z — title (YYYY-MM-DD[, note])`. Pure: no npm imports.

const HEADING = /^## v(\d+\.\d+\.\d+) — (.+) \((\d{4}-\d{2}-\d{2})(?:, ([^)]+))?\)\s*$/;
const FOLDED = /never published; shipped in v(\d+\.\d+\.\d+)/;

/** Every release section, in file order: `{ version, title, date, shippedIn, body }`. */
export function parseReleases(history) {
  const releases = [];
  let current = null;
  for (const line of history.split('\n')) {
    if (line.startsWith('## ')) {
      const match = HEADING.exec(line);
      // A release heading that is not shaped right would drop its whole section
      // from the changelog without a word. Notes from before the first major version are not releases.
      if (!match && /^## v[1-9]\d*\.\d+\.\d+\b/.test(line)) {
        throw new Error(
          `version-history.md: release heading is not "## vX.Y.Z — title (YYYY-MM-DD[, note])": ${line}`,
        );
      }
      current = match
        ? {
            version: match[1],
            title: match[2],
            date: match[3],
            shippedIn: FOLDED.exec(match[4] ?? '')?.[1] ?? null,
            body: [],
          }
        : null;
      if (current) releases.push(current);
    } else if (current) {
      current.body.push(line);
    }
  }
  return releases.map((release) => ({ ...release, body: release.body.join('\n').trim() }));
}

export function compareVersions(a, b) {
  const pa = a.split('.').map(Number);
  const pb = b.split('.').map(Number);
  for (let i = 0; i < 3; i++) if (pa[i] !== pb[i]) return pa[i] - pb[i];
  return 0;
}

/**
 * Published releases, newest first. A version that was never published is not a
 * release: it is listed under the release that shipped its work, as `includes`.
 * A version folded into a release that has no section of its own is an error.
 */
export function publishedReleases(history) {
  const all = parseReleases(history);
  const published = all
    .filter((release) => release.shippedIn === null)
    .map((release) => ({ ...release, includes: [] }));
  for (const folded of all.filter((release) => release.shippedIn !== null)) {
    const carrier = published.find((release) => release.version === folded.shippedIn);
    if (!carrier) {
      throw new Error(
        `version-history.md: v${folded.version} says it shipped in v${folded.shippedIn}, ` +
          'which has no release section',
      );
    }
    carrier.includes.push(folded);
  }
  for (const release of published) {
    release.includes.sort((a, b) => compareVersions(b.version, a.version));
  }
  return published.sort((a, b) => compareVersions(b.version, a.version));
}
