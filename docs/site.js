// skep site — shared header/footer + behaviors. No build step; plain JS.
(() => {
  const GH_REPO = 'Anmolnoor/skep';
  const GH_URL = 'https://github.com/' + GH_REPO;
  const VERSION = 'v1.0.1'; // fallback; refreshed live from PyPI below
  const CONTRACT = 'worker contract 0.3.5';

  const LOGO_SVG =
    '<svg width="30" height="30" viewBox="0 0 32 32" fill="none" aria-hidden="true">' +
    '<path d="M16 1.6 29.2 9.2v15.2L16 32 2.8 24.4V9.2L16 1.6Z" fill="#FF6B1F"></path>' +
    '<path d="M16 9.4 22.6 13.2v7.6L16 24.6 9.4 20.8v-7.6L16 9.4Z" stroke="#0A0B0C" stroke-width="1.8" fill="none"></path>' +
    '</svg>';

  const NAV = [
    ['how-it-works', 'how-it-works.html', 'How it works'],
    ['security', 'security.html', 'Security'],
    ['agents', 'agents.html', 'Agents'],
    ['cli', 'cli.html', 'CLI'],
    ['docs', 'docs.html', 'Docs'],
    ['changelog', 'changelog.html', 'Changelog'],
    ['open-source', 'open-source.html', 'Open Source'],
  ];

  function header(active) {
    const links = NAV.map(
      ([id, href, label]) =>
        `<a href="./${href}"${id === active ? ' class="active"' : ''}>${label}</a>`
    ).join('');
    return `
<header class="site-header">
  <a href="./index.html" class="brand" aria-label="skep home">${LOGO_SVG}<span>skep</span></a>
  <nav class="site-nav" aria-label="Primary">${links}</nav>
  <div class="header-actions">
    <a href="${GH_URL}" class="gh-btn">
      <i data-lucide="github"></i><span>GitHub</span>
      <span class="gh-meta"><span data-stat="stars-inline"></span><span data-version>${VERSION}</span></span>
    </a>
    <a href="./install.html" class="install-btn"><span>Install Skep</span><i data-lucide="arrow-right"></i></a>
    <button class="menu-btn" aria-label="Menu" aria-expanded="false"><i data-lucide="menu"></i></button>
  </div>
</header>
<nav class="mobile-nav" aria-label="Primary mobile">
  ${NAV.map(([, href, label]) => `<a href="./${href}">${label}</a>`).join('')}
  <a href="${GH_URL}">GitHub</a>
</nav>`;
  }

  function footer() {
    return `
<footer class="site-footer">
  <div class="footer-grid">
    <div style="min-width:0">
      <a href="./index.html" class="brand" style="margin-bottom:14px">${LOGO_SVG}<span>skep</span></a>
      <p style="margin:0 0 20px;max-width:210px;font-size:14px;line-height:1.55;color:var(--text-faint)">Govern agents. Protect your code.</p>
      <div class="footer-social">
        <a href="${GH_URL}" aria-label="GitHub"><i data-lucide="github"></i></a>
        <a href="https://pypi.org/project/skep/" aria-label="PyPI"><i data-lucide="package"></i></a>
        <a href="./security.html" aria-label="Security contact"><i data-lucide="shield"></i></a>
      </div>
    </div>
    <div style="min-width:0">
      <div class="footer-col-title">Product</div>
      <div class="footer-links">
        <a href="./how-it-works.html">How it works</a>
        <a href="./security.html">Security</a>
        <a href="./agents.html">Agents</a>
        <a href="./cli.html">CLI</a>
        <a href="./install.html">Install</a>
      </div>
    </div>
    <div style="min-width:0">
      <div class="footer-col-title">Resources</div>
      <div class="footer-links">
        <a href="./docs.html">Docs</a>
        <a href="./changelog.html">Changelog</a>
        <a href="./open-source.html">Open Source</a>
        <a href="./roadmap.html">Roadmap</a>
        <a href="./blog.html">Blog</a>
      </div>
    </div>
    <div style="min-width:0">
      <div class="footer-col-title">Legal</div>
      <div class="footer-links">
        <a href="./security.html">Security</a>
        <a href="./privacy.html">Privacy</a>
        <a href="${GH_URL}/blob/main/LICENSE">License (MIT)</a>
        <a href="./code-of-conduct.html">Code of Conduct</a>
      </div>
    </div>
    <div style="min-width:0;align-self:start;padding:20px;border:1px solid var(--line-card);border-radius:12px;background:var(--bg-card)">
      <div style="margin-bottom:7px;font-size:14.5px;font-weight:650;color:var(--text-hi)">Questions or feedback?</div>
      <p style="margin:0 0 14px;font-size:13.5px;line-height:1.55;color:var(--text-faint)">Open an issue, or start a discussion on GitHub.</p>
      <a href="${GH_URL}/discussions" class="link-arrow" style="font-size:13.5px"><span>Start a discussion</span><i data-lucide="arrow-right"></i></a>
    </div>
  </div>
  <div class="footer-bottom">
    <span>© 2026 Skep. Released under the MIT License.</span>
    <span class="mono" style="font-size:12px">${CONTRACT}</span>
  </div>
</footer>`;
  }

  function fmt(n) {
    return n >= 1000 ? (n / 1000).toFixed(n >= 10000 ? 0 : 1) + 'k' : String(n);
  }

  async function cachedJson(key, url, ttlMs) {
    try {
      const hit = JSON.parse(sessionStorage.getItem(key) || 'null');
      if (hit && Date.now() - hit.t < ttlMs) return hit.v;
    } catch {}
    const res = await fetch(url);
    if (!res.ok) throw new Error(url + ' -> ' + res.status);
    const v = await res.json();
    try { sessionStorage.setItem(key, JSON.stringify({ t: Date.now(), v })); } catch {}
    return v;
  }

  // Live numbers: GitHub repo stats + released PyPI version. Fallbacks stay
  // if either fetch fails (offline, rate limit) — elements keep their
  // hardcoded text and [data-stat] placeholders stay hidden.
  async function liveStats() {
    try {
      const repo = await cachedJson('skep:gh', 'https://api.github.com/repos/' + GH_REPO, 3600e3);
      const map = {
        stars: repo.stargazers_count,
        forks: repo.forks_count,
        issues: repo.open_issues_count,
        watchers: repo.subscribers_count,
      };
      for (const [k, v] of Object.entries(map)) {
        if (v == null) continue;
        document.querySelectorAll(`[data-stat="${k}"]`).forEach((el) => { el.textContent = fmt(v); });
      }
      document.querySelectorAll('[data-stat="stars-inline"]').forEach((el) => {
        el.textContent = '★ ' + fmt(map.stars) + ' · ';
      });
      if (repo.pushed_at) {
        document.querySelectorAll('[data-stat="pushed"]').forEach((el) => {
          el.textContent = new Date(repo.pushed_at).toISOString().slice(0, 10);
        });
      }
    } catch {}
    try {
      const pypi = await cachedJson('skep:pypi', 'https://pypi.org/pypi/skep/json', 3600e3);
      const v = pypi && pypi.info && pypi.info.version;
      if (v) document.querySelectorAll('[data-version]').forEach((el) => { el.textContent = 'v' + v; });
    } catch {}
  }

  function wireCopy() {
    document.addEventListener('click', (e) => {
      const btn = e.target.closest && e.target.closest('.copy-btn');
      if (!btn) return;
      const scope = btn.closest('[data-copy-scope]');
      const pre = scope && scope.querySelector('pre');
      if (!pre) return;
      const text = pre.innerText.replace(/^\$\s+/gm, '').trim();
      if (navigator.clipboard) navigator.clipboard.writeText(text);
      const prev = btn.innerHTML;
      btn.innerHTML = '<span style="font-size:11px;font-weight:600;color:var(--green)">✓</span>';
      setTimeout(() => { btn.innerHTML = prev; icons(); }, 1400);
    }, true);
  }

  function wireReveal() {
    const els = document.querySelectorAll('.reveal');
    if (!els.length || !('IntersectionObserver' in window)) {
      els.forEach((el) => el.classList.add('shown'));
      return;
    }
    const obs = new IntersectionObserver((entries) => {
      entries.forEach((en) => {
        if (!en.isIntersecting) return;
        en.target.classList.add('shown');
        obs.unobserve(en.target);
      });
    }, { rootMargin: '0px 0px -12% 0px', threshold: 0.06 });
    els.forEach((el) => obs.observe(el));
  }

  function icons() { if (window.lucide) window.lucide.createIcons(); }

  function init() {
    const active = document.body.dataset.page || '';
    document.body.insertAdjacentHTML('afterbegin', header(active));
    document.body.insertAdjacentHTML('beforeend', footer());

    const menuBtn = document.querySelector('.menu-btn');
    const mobileNav = document.querySelector('.mobile-nav');
    if (menuBtn && mobileNav) {
      menuBtn.addEventListener('click', () => {
        const open = mobileNav.classList.toggle('open');
        menuBtn.setAttribute('aria-expanded', String(open));
      });
    }

    icons();
    wireCopy();
    wireReveal();
    liveStats();
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
