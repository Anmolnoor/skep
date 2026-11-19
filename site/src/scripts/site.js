// The one script every page loads: copy buttons, on-demand search, and the live
// PyPI / GitHub refresh. Everything here improves a page that already works
// without it: build-time values are the fallback when a fetch fails.
const GH_REPO = 'Anmolnoor/skep';

// ---- copy buttons (CodeBlock, and every code block in rendered markdown) ----
const COPY_ICON =
  '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect width="14" height="14" x="8" y="8" rx="2" ry="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/></svg>';

for (const pre of document.querySelectorAll('.prose pre')) {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'copy-btn';
  button.dataset.copy = '';
  button.setAttribute('aria-label', 'Copy to clipboard');
  button.innerHTML = COPY_ICON;
  pre.append(button);
}

document.addEventListener('click', async (event) => {
  const button = event.target.closest?.('[data-copy]');
  if (!button) return;
  const pre = button.closest('pre') ?? button.closest('[data-copy-scope]')?.querySelector('pre');
  if (!pre) return;
  const code = pre.querySelector('code') ?? pre;
  try {
    await navigator.clipboard.writeText(code.innerText.replace(/^\$ /gm, '').trim());
  } catch {
    return; // no clipboard permission: leave the button as it was
  }
  button.dataset.copied = '';
  const previous = button.innerHTML;
  button.textContent = 'copied';
  setTimeout(() => {
    delete button.dataset.copied;
    button.innerHTML = previous;
  }, 1400);
});

// ---- search: Pagefind's component UI, loaded on the first open ----
let searchReady;
function loadSearch() {
  searchReady ??= (async () => {
    const css = document.createElement('link');
    css.rel = 'stylesheet';
    css.href = '/pagefind/pagefind-component-ui.css';
    document.head.append(css);
    // Not an import the bundler can follow: the file exists only after `pagefind --site dist`.
    const url = '/pagefind/pagefind-component-ui.js';
    await import(/* @vite-ignore */ url);
    await customElements.whenDefined('pagefind-modal');
    return document.querySelector('pagefind-modal');
  })();
  return searchReady;
}
async function openSearch() {
  try {
    (await loadSearch()).open();
  } catch {
    searchReady = undefined; // `astro dev` has no index: let the next click try again
  }
}
document.addEventListener('click', (event) => {
  if (event.target.closest?.('[data-search-open]')) openSearch();
});
document.addEventListener('keydown', (event) => {
  // Once the UI is loaded it owns this shortcut itself.
  if (searchReady || !(event.metaKey || event.ctrlKey) || event.key.toLowerCase() !== 'k') return;
  event.preventDefault();
  openSearch();
});

// ---- live numbers: [data-version] from PyPI, [data-stat] from the GitHub API ----
const compact = (n) => (n >= 1000 ? `${(n / 1000).toFixed(n >= 10000 ? 0 : 1)}k` : String(n));

async function cachedJson(key, url, ttlMs) {
  try {
    const hit = JSON.parse(sessionStorage.getItem(key) || 'null');
    if (hit && Date.now() - hit.t < ttlMs) return hit.v;
  } catch {}
  const response = await fetch(url);
  if (!response.ok) throw new Error(`${url} -> ${response.status}`);
  const value = await response.json();
  try {
    sessionStorage.setItem(key, JSON.stringify({ t: Date.now(), v: value }));
  } catch {}
  return value;
}

function setAll(selector, text) {
  for (const element of document.querySelectorAll(selector)) element.textContent = text;
}

async function liveStats() {
  try {
    const repo = await cachedJson('skep:gh', `https://api.github.com/repos/${GH_REPO}`, 3600e3);
    const stats = {
      stars: repo.stargazers_count,
      forks: repo.forks_count,
      issues: repo.open_issues_count,
      watchers: repo.subscribers_count,
    };
    for (const [name, value] of Object.entries(stats)) {
      if (value != null) setAll(`[data-stat="${name}"]`, compact(value));
    }
    if (stats.stars != null) setAll('[data-stat="stars-inline"]', `★ ${compact(stats.stars)} · `);
    if (repo.pushed_at) setAll('[data-stat="pushed"]', new Date(repo.pushed_at).toISOString().slice(0, 10));
    document.dispatchEvent(new CustomEvent('skep:stats', { detail: stats }));
  } catch {}
  try {
    const pypi = await cachedJson('skep:pypi', 'https://pypi.org/pypi/skep/json', 3600e3);
    if (pypi?.info?.version) setAll('[data-version]', `v${pypi.info.version}`);
  } catch {}
}
liveStats();
