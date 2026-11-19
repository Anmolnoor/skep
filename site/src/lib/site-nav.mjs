// The one list of pages the header and footer link (plan §3.4). The foundation
// owns this file: a page agent that needs a nav change asks for it.
// tests/supervisor/test_site_sources.py checks every page under src/pages is listed.
import { GH_URL } from './links.mjs';

export { GH_REPO, GH_URL } from './links.mjs';
export const ISSUES_URL = `${GH_URL}/issues`;
export const PYPI_URL = 'https://pypi.org/project/skep/';

/** Header navigation: `{ id, href, label }`; `id` matches a page's `active` prop. */
export const NAV = [
  { id: 'why-skep', href: '/why-skep.html', label: 'Why skep' },
  { id: 'how-it-works', href: '/how-it-works.html', label: 'How it works' },
  { id: 'security', href: '/security.html', label: 'Security' },
  { id: 'agents', href: '/agents.html', label: 'Agents' },
  { id: 'cli', href: '/cli.html', label: 'CLI' },
  { id: 'guides', href: '/guides.html', label: 'Guides' },
  { id: 'docs', href: '/docs.html', label: 'Docs' },
  { id: 'changelog', href: '/changelog.html', label: 'Changelog' },
];

/** Footer columns. Together with NAV they reach every page of the site. */
export const FOOTER = [
  {
    title: 'Product',
    links: [
      { href: '/why-skep.html', label: 'Why skep' },
      { href: '/how-it-works.html', label: 'How it works' },
      { href: '/security.html', label: 'Security' },
      { href: '/agents.html', label: 'Agents' },
      { href: '/cli.html', label: 'CLI' },
    ],
  },
  {
    title: 'Get started',
    links: [
      { href: '/install.html', label: 'Install' },
      { href: '/first-run.html', label: 'First run' },
      { href: '/guides.html', label: 'Guides' },
      { href: '/docs.html', label: 'Docs' },
    ],
  },
  {
    title: 'Project',
    links: [
      { href: '/changelog.html', label: 'Changelog' },
      { href: '/roadmap.html', label: 'Roadmap' },
      { href: '/open-source.html', label: 'Open Source' },
      { href: '/blog.html', label: 'Blog' },
    ],
  },
  {
    title: 'Legal',
    links: [
      { href: '/privacy.html', label: 'Privacy' },
      { href: `${GH_URL}/blob/main/LICENSE`, label: 'License (MIT)' },
      { href: '/code-of-conduct.html', label: 'Code of Conduct' },
    ],
  },
];
