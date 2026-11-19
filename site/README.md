# skep docs site

The source of https://skep.anmolnoor.com: an Astro 7 static site. It is the one
place in this repository with npm. The app UI stays no-build, and pytest never
needs `site/node_modules` or `site/dist`.

Content comes from the repository around it, at build time:

| What the site shows | Where it comes from |
|---|---|
| Version (header, footer, "latest" chip) | `version` in `../pyproject.toml` |
| Worker contract version (footer) | `CONTRACT_VERSION` in `../src/skep/worker_contract/version.py` |
| Docs sidebar and the hub `/docs.html` | the sections of `../docs/README.md` |
| Every `/docs/<id>.html` page | every `../docs/**/*.md` file; the title is its first `# ` heading |
| `/changelog.html` | the `## vX.Y.Z — title (date)` sections of `../docs/version-history.md` |
| The numbers strip on the home page | counted at build by `homeNumbers()` in `src/lib/facts.mjs`: the keys of `CODING_ENGINES` in `../src/skep/supervisor/engines.py`, the outcomes passed to `ReverifyOutcome(` in `reverify.py`, the backends `sandbox.py` can report as available, the `**I<n>.` paragraphs of `../docs/invariants.md`, the `NNNN-*.md` files of `../docs/adr/` |

A reader that cannot parse its source throws, so the build fails instead of
printing a wrong number. The home page also fails the build when its engines
table lists different ids from the registry.

To add a doc, add the markdown file under `docs/` and list it in
`docs/README.md`. Nothing in `site/` changes.

## Install, run, build

Node 24 (22.23.4 or newer works) and npm. Run every command inside `site/`.

```sh
npm ci            # install exactly what package-lock.json pins
npm run dev       # http://localhost:4321, live reload (search has no index in dev)
npm run build     # astro build into dist/, then the Pagefind search index
npm test          # node --test on src/lib/*.test.mjs (works before npm ci)
npm run check     # link check over dist/ and the home page JS budget
npm run preview   # serve dist/ locally
```

The build downloads the two font files from Fontsource the first time (Astro
caches them under `node_modules/.astro`), so the first build needs the network.

`npm run check` reads `dist/`: build first. It fails when

- a relative or root-absolute `href`/`src` does not resolve, or its `#hash` names no id on the target page;
- a page of the old site (`agents.html`, `docs.html`, …) is missing from `dist/`;
- an old `doc.html?d=<slug>` no longer resolves;
- a page links GitHub Discussions (disabled on the repository: link Issues);
- the home page loads more than 160 KB of JavaScript, gzipped.

## Layout

```
astro.config.mjs        site URL, build.format 'file', fonts, markdown, the docs copy step
src/content.config.ts   collection "docs" = ../docs/**/*.md
src/lib/*.mjs           pure build helpers, node builtins only, each with a *.test.mjs
src/lib/site-nav.mjs    header and footer links
src/lib/repo.mjs        reads the version and contract from the Python sources
src/plugins/            the Sätteri markdown wrapper (may import npm packages)
src/styles/             tokens.css, base.css (type, layout, motion), kit.css, docs.css
src/components/         the kit (Astro components)
src/components/reactbits/   four text animations, React, under their own licence
src/components/islands/     React islands that belong to one page
src/layouts/            Base.astro, Doc.astro
src/pages/              one .astro per URL: /x.html is src/pages/x.astro
src/scripts/site.js     the one script every page loads
scripts/                check-links.mjs, check-js-budget.mjs
```

URLs are root-absolute and end in `.html`: write `href="/security.html"`, never
`./security.html` or `/security`. The home page is `/`.

## Who edits what

A page author edits **only** the files their branch owns:

- their own `src/pages/<name>.astro` files;
- their own islands under `src/components/islands/` (create the file; name it after the page);
- the markdown under `docs/` that their branch owns.

A page author does **not** edit: `astro.config.mjs`, `package.json`,
`package-lock.json`, `src/layouts/`, `src/styles/`, `src/components/*.astro`,
`src/components/reactbits/`, `src/lib/`, `src/plugins/`, `src/scripts/`,
`src/content.config.ts`, `scripts/`, `src/pages/kit.astro`, `src/pages/doc.astro`,
`src/pages/docs.astro`, `src/pages/docs/`, `src/pages/changelog.astro`, the
workflow, or any test. If a page needs a kit change, a new dependency, a nav
entry or a new token, say so in the report; the foundation owner makes it.

Page-local CSS goes in a `<style>` block in the page (Astro scopes it). Use the
tokens; add no colours, font sizes, radii or durations of your own.

## Writing a page

```astro
---
import Base from '../layouts/Base.astro';
import Section from '../components/Section.astro';
import Card from '../components/Card.astro';
---

<Base title="Security" description="One sentence for search engines." active="security">
  <Section eyebrow="Threat model" title="What the sandbox stops" lead="One lead sentence.">
    <div class="grid grid-3">
      <Card title="Filesystem" icon="folder-lock">Body text.</Card>
    </div>
  </Section>
  <Section band title="Limits">…</Section>
</Base>
```

Rules the tests and the design contract enforce:

- **Every page uses `Base`** (`tests/supervisor/test_site_sources.py`). `Base` props:
  `title` (required; " — skep" is appended), `description` (required), `active`
  (the header nav id: `why-skep`, `how-it-works`, `security`, `agents`, `cli`,
  `guides`, `docs`, `changelog`; omit it for pages that are not in the header),
  `home` (the home page only: the title is used as given), `noindex`.
  A named slot `head` puts extra tags in `<head>`.
- **One `<h1>` per page**, written by the page. Inner pages use `class="t-h2"` on
  it; only the home page hero uses `class="t-display"`.
- **Stubs.** A foundation stub carries the line "This page is being written.".
  Remove that line when you write the page: the tests treat a page that still
  has it as a stub and skip the content pins for it.
- **Sections alternate** plain and `band`. One eyebrow + h2 + lead per section.
  Running text is at most 72 characters wide (`class="prose-width"`).
- **No typed versions.** `v1.2.3`, the current version and the contract number
  must not appear in `src/` (a test fails). Read them at build:
  `import { siteFacts } from '../lib/repo.mjs'; const { version, contract } = siteFacts();`
  For text the live PyPI refresh should update, render `<span data-version>v{version}</span>`.
- **Live numbers.** `src/scripts/site.js` fills `[data-version]` from PyPI and
  `[data-stat="stars" | "forks" | "issues" | "watchers" | "pushed"]` from the
  GitHub API after load. Put the build-time or neutral fallback inside the
  element; it stays if the fetch fails. The script dispatches a `skep:stats`
  event on `document` (detail: the numbers) for an island that wants them.
- **`skep run` examples carry `--execution-mode`**, `--home` goes before the
  subcommand, and every `skep <command> [<subcommand>]` named in `cli.astro`
  must exist in the real CLI (`tests/supervisor/test_site_facts.py`).
- **Banned phrasings** (a test fails, any case): "never pushes", "nothing lands
  without", "tamper-proof", "tamper proof", "fully sandboxed", "planned
  adapters", and any link to GitHub Discussions. Link Issues:
  `import { ISSUES_URL, GH_URL, PYPI_URL } from '../lib/site-nav.mjs'`.
- **Contrast.** Text uses `--text-faint` or brighter. `--text-ghost` and
  `--text-void` are for borders and rules only; `color: var(--text-void)` fails a test.
- **Contact address.** Only `security.astro` and `code-of-conduct.astro` may
  carry the published contact email (the release hygiene scan allows those two files).
- **Search.** `Base` marks `<main>` with `data-pagefind-body`, so every page is
  indexed. Add `data-pagefind-ignore` to an element that should not be searchable:
  an illustration, a decorative island, a number badge set against a word (the
  index glues them: "2skep"). `Diff` ignores its own lines, and the two page
  islands (`RunPanel`, `PolicyTrace`) ignore themselves.
- **Transcripts.** No invented output. Pass real captured text to `Terminal` and
  say where it came from in `captured`.

## Tokens

All in `src/styles/tokens.css`. Use the variables, never the raw values.

- **Colour.** Surfaces `--bg`, `--bg-alt` (bands), `--bg-card`, `--bg-panel`,
  `--bg-inset`, `--bg-chip`, `--bg-hover`. Lines `--line-hair`, `--line-inner`,
  `--line-card` (default 1px border), `--line-strong`, `--line-hover` (hover
  border). Text, brightest first: `--text-bright`, `--text-hi`, `--text`,
  `--text-soft`, `--text-mid`, `--text-muted`, `--text-dim`, `--text-faint`.
  Meaning: `--orange` (skep), `--green` (verified), `--red` (risk), `--yellow` (limit).
- **Type**, seven sizes and no others, as classes: `t-display`, `t-h2`, `t-h3`,
  `t-lead`, `t-body`, `t-small`, `t-micro` (mono, uppercase). Variables
  `--fs-display` … `--fs-micro` for page-local CSS. Fonts: `--sans`, `--mono`.
- **Space** `--s-1` … `--s-9` = 4, 8, 12, 16, 24, 32, 48, 64, 96 px. Section
  padding is `--pad-y`; header to content 32; card gap 16; card padding 24.
- **Radius** `--r-chip` 6, `--r-card` 10, `--r-panel` 12.
- **Motion** `--dur-1` 120ms (hover), `--dur-2` 240ms (state), `--dur-3` 480ms
  (reveal), `--dur-4` 900ms (hero step); `--ease-out`, `--ease-in-out`;
  `--stagger-char` 40ms, `--stagger-word` 80ms, `--stagger-row` 60ms, at most 12 steps.

Layout classes (`src/styles/base.css`): `wrap` (page gutter, 1400px), `wrap narrow`
(920px), `section`, `band`, `grid` with `grid-2` / `grid-3` / `grid-4` (at most
that many columns, fewer on small screens), `stack` (16px between children,
panels included), `row` (wrapping flex row), `prose-width` (72ch), `sr-only`, `no-js-hide`,
`reveal`, `stage-rows`, `link-arrow`, `atmos` (see Atmosphere), and the
transcript colours `c-green`, `c-red`, `c-yellow`, `c-orange`, `c-dim`, `c-mut`.

## Kit

Every component is rendered on `/kit.html` (`src/pages/kit.astro`).
Icons are Lucide names in kebab-case (https://lucide.dev/icons), plus `github` and `skep`.

### Section

One band of a page with its head.

| Prop | Type | Default | |
|---|---|---|---|
| `id` | string | | anchor id |
| `band` | boolean | false | alternate background |
| `eyebrow` | string | | mono label above the heading |
| `tone` | `orange` \| `green` \| `red` \| `yellow` | orange | eyebrow colour |
| `title` | string | | rendered as `<h2 class="t-h2">` |
| `lead` | string | | one sentence under the heading |
| `narrow` | boolean | false | 920px measure |
| `reveal` | boolean | false | CSS scroll reveal of the body; never on a page's first section |

Slots: default (the body); `heading` replaces the `<h2>` (for a `BlurText` island,
or the page's `<h1 class="t-h2">` in the first Section of an inner page).

```astro
<Section id="limits" band eyebrow="Limits" tone="yellow" title="What it does not do" lead="…" reveal>
  <p class="prose-width">…</p>
</Section>
```

### Eyebrow

`tone`: `orange` (default) \| `green` \| `red` \| `yellow`.

```astro
<Eyebrow tone="green">Verified</Eyebrow>
```

### Button

| Prop | Type | Default | |
|---|---|---|---|
| `href` | string | | renders `<a>`; without it a `<button>` |
| `variant` | `primary` \| `ghost` | primary | |
| `size` | `md` \| `sm` | md | |
| `icon` | string | | Lucide name, shown after the label |
| `type` | `button` \| `submit` | button | only without `href` |

```astro
<Button href="/install.html" icon="arrow-right">Install</Button>
<Button href={GH_URL} variant="ghost" size="sm" icon="github">Source</Button>
```

### Card

| Prop | Type | Default | |
|---|---|---|---|
| `title` | string | | |
| `icon` | string | | Lucide name, in a badge above the title |
| `tone` | `orange` \| `green` \| `red` \| `yellow` | orange | badge colour |
| `href` | string | | makes the whole card one link |
| `level` | `h2` \| `h3` \| `h4` | h3 | heading level of the title |

```astro
<Card title="Re-verification" icon="shield-check" tone="green">Body text.</Card>
```

### Chip

`kind` (required): `allow` \| `ask` \| `deny` (verdicts, uppercase) \| `ok` \| `risk` \| `limit`.
The slot is the label; it defaults to the kind.

```astro
<Chip kind="deny" /> <Chip kind="ok">confirmed</Chip> <Chip kind="limit">workspace mode</Chip>
```

### Callout

`kind`: `limit` (default, yellow) \| `risk` \| `ok` \| `note`; `title` optional.
State every limit of a claim in a `limit` callout next to the claim.

```astro
<Callout kind="limit" title="Limit">The hook advises and records; it does not sandbox.</Callout>
```

### Terminal

A captured transcript. Lines starting `$ ` are commands, `# ` are notes, the rest is output.

| Prop | Type | Default | |
|---|---|---|---|
| `code` | string | required | the transcript; keep it in a template literal in the frontmatter |
| `captured` | string | required | provenance, printed under the transcript |
| `title` | string | terminal | |
| `caret` | boolean | false | blinking CSS cursor after the last line |
| `wrap` | boolean | false | wrap long lines instead of scrolling sideways |

```astro
---
const doctor = `
$ skep doctor
…the real output…
`;
---
<Terminal code={doctor} title="skep doctor" captured="skep doctor, throwaway SKEP_HOME, 2026-11-18" />
```

### Diff

| Prop | Type | Default | |
|---|---|---|---|
| `code` | string | required | unified diff text; lines are coloured by `+`, `-`, `@@` |
| `file` | string | patch | name in the head |
| `stage` | boolean | false | lines stage in as the block scrolls into view (CSS only) |
| `landed` | string | | line under the diff |
| `captured` | string | | provenance, printed under the diff |

File headers (`--- a/x`, `+++ b/x`) are shown as context, not as a removed and an added line.

Running text on a page: put the class `copy` on the section (or `main`) and inline
`<code>` and plain `<a>` inside `p`, `li`, `td` and `dd` get the kit's inline styles.

```astro
<Diff code={patch} file="src/app.py" stage landed="skep/<task> ← patch · main untouched" />
```

### Ledger

Rows from a decision log or a run record.

| Prop | Type | Default | |
|---|---|---|---|
| `rows` | `{ when?, what, detail?, verdict?, label? }[]` | required | `verdict` is a Chip kind; `label` overrides the chip text |
| `title` | string | decisions | |
| `captured` | string | | provenance, as for Terminal |
| `stage` | boolean | false | rows stage in on scroll (CSS only) |

```astro
<Ledger title="decisions.jsonl" captured="skep policy decisions, 2026-11-18"
  rows={[{ when: '12:00:01', what: 'git push origin main', detail: 'floor', verdict: 'deny' }]} />
```

### CodeBlock

Highlighted code (Shiki, at build) with a copy button. Copying strips a leading `$ `.

| Prop | Type | Default | |
|---|---|---|---|
| `code` | string | required | |
| `lang` | string | sh | Shiki language id |
| `title` | string | the lang | |
| `step` | number | | numbered badge before the title |
| `copy` | boolean | true | |

```astro
<CodeBlock code={`$ pipx install skep\n$ skep onboard`} title="Install" step={1} />
```

### Table

Scrolls sideways inside its own box. Either `head` + `rows` (plain text cells) or a slot with `<thead>`/`<tbody>`.

| Prop | Type | Default | |
|---|---|---|---|
| `head` | string[] | | column headers |
| `rows` | string[][] | | |
| `caption` | string | | shown under the table; also the region's accessible name |
| `rowHeaders` | boolean | false | first cell of each row is a row header |
| `stack` | boolean | false | under 640px each row is a card and each cell sits under its column label, instead of the table scrolling sideways |

```astro
<Table caption="Engines" head={['Engine', 'Auth']} rows={[['codex', 'OPENAI_API_KEY']]} rowHeaders stack />
```

`stack` keeps the table a table for a screen reader: the `<thead>` is hidden
from sight only, and the table roles are written out because `display: block`
on a row drops them in some browsers. With `head` + `rows` the labels and roles
are written for you. With slotted markup, write them yourself:

```astro
<Table caption="Engines" stack>
  <thead role="rowgroup"><tr role="row"><th scope="col" role="columnheader">Engine</th><th scope="col" role="columnheader">Auth</th></tr></thead>
  <tbody role="rowgroup">
    <tr role="row"><th scope="row" role="rowheader">codex</th><td role="cell" data-label="Auth"><code>OPENAI_API_KEY</code></td></tr>
  </tbody>
</Table>
```

Use `stack` instead of a page-local copy of this CSS.

### Panels in a stack

`Terminal`, `Diff`, `Ledger` and `CodeBlock` are `<figure class="panel">`. The
figure margin is cleared at zero specificity, so a panel inside a `.stack` (or
under any page rule) is spaced like any other child. Do not wrap a panel in a
`<div>` to get its spacing back.

### Atmosphere

The brand's fine grid under a radial mask, with one soft orange glow: CSS only,
static, never cursor-reactive. Both layers are pseudo-elements, so they are not
in the accessibility tree.

- `class="atmos"` on a section is the home hero's strength.
- **The page header gets a quieter one for free.** Every inner page starts with a
  Section whose `heading` slot holds the page's `<h1 class="t-h2">`; `kit.css`
  finds that pattern (`main > .section:first-child` with an h1 in its head) and
  draws the grid and glow behind it. A page does nothing to opt in.

```astro
<section class="hero atmos">…</section>
```

### Search

The trigger button. `Base` already puts one in the header; add another only on a page that leads with search.
Props: `label` (default "Search"), `wide` (full-width box). Hidden without JavaScript.

```astro
<Search label="Search the documentation" wide />
```

### Icon

Props: `name` (required), `size` (default 16), `label` (accessible name; without it the icon is `aria-hidden`), `class`.
An unknown name fails the build.

```astro
<Icon name="shield-check" size={18} /> <Icon name="github" label="GitHub" />
```

### Header, Footer, DocsSidebar

Rendered by the layouts; pages do not use them. Their links are `NAV` and `FOOTER` in `src/lib/site-nav.mjs`.

### Layouts

`Base` is described under "Writing a page". `Doc` (sidebar, on-page contents, previous/next, edit on GitHub) is used by `src/pages/docs/[...id].astro` only. On a doc page running text is 33rem wide, about 68 characters of real text; code blocks and tables keep the whole column (`docs.css`). `--prose` (72ch) measures the digit 0, which is wider than the average letter: it comes to about 93 characters.

## Motion

- **M1** Text animates only when it is data changing state (a verdict, a number, a run step). Never body text, nav, the h1, buttons or legal text.
- **M2** At most one animated text element per viewport; each plays once; no loops except the terminal cursor.
- **M3** Server HTML is the final state. Animation styles apply only under `html.js` (set inline in `<head>` by `Base`) and outside `prefers-reduced-motion: reduce`. **Nothing rewinds when an island hydrates**: what is on screen never goes from its final state back to an earlier one. An island that is in the viewport when it hydrates keeps its final state and does not play; it plays only when it scrolls into view later. An island that must play above the fold (the home hero) paints its pre-play state from the first frame with CSS, and lets go of it with CSS after a few seconds if the island never hydrates (`RunPanel.css` is the pattern).
- **M4** Animate emphasis, never existence: nothing mounts or unmounts on animation; reserve height.
- **M5** Section reveals are CSS: opacity and a transform of at most 16px, `animation-timeline: view()` where supported, never a blur on a large area. Use `class="reveal"` (or `<Section reveal>`) and `class="stage-rows"` with `style="--i: N"` on each child.
- **M6** Islands: one React island per page by default; `client:visible` unless it is above the fold (`client:idle`). Focus order follows the DOM; a decorative split is `aria-hidden` and has a visible text twin. The home page is the exception: its hero panel, its section headings, its numbers and its engine line are islands, inside the JavaScript budget below.

**Writing a scroll-driven animation of your own.** Put `animation-timeline` and
`animation-range` in a rule of their own, under a selector the minifier cannot
merge with the rule that sets `animation` (append `:where(*)`, which adds no
specificity):

```css
html.js .thing { animation: thing-in linear both; }
html.js .thing:where(*) { animation-timeline: view(); animation-range: entry 0% entry min(25%, 80px); }
```

Written in one rule, the build's CSS minifier folds the timeline into the
`animation` shorthand, browsers reject that declaration, and the animation
silently never runs in the built site while it works in `npm run dev`. Check a
scroll animation in `npm run build` output, not only in dev.

The same minifier keeps only one of a prefixed and an unprefixed declaration of
one property written in one rule (`-webkit-backdrop-filter` and
`backdrop-filter`): split them the same way. The header's blur was missing in
every Chromium build until it was (`kit.css`, `.site-header::before`).

A reveal is fully opaque once a quarter of the block is in, and never later
than 80px in, so a block that rests low in the viewport is not half-faded.

Page transitions are the CSS rule `@view-transition { navigation: auto; }` in
`base.css`. No `<ClientRouter />`, no vanilla motion scripts. Do not use: aurora
or gradient meshes, cursor-reactive particle fields, glitch text, a typewriter
h1, logo marquees, tilt-on-hover grids, cursor trails, scroll-jacking. The
hexagon is the mark, not a background pattern.

When you write your own island, use `motion` 14 through `motion/react`, render
the final text on the server, and check `prefers-reduced-motion` before starting.

### The four text animations

Copied from React Bits into `src/components/reactbits/` and patched (each change
is marked `skep:` in the file). They are React components: use them inside your
own island, or directly in a page with a client directive. All four render
their final text on the server and hold it under reduced motion.

| Component | Use it for | Key props | Notes |
|---|---|---|---|
| `DecryptedText` | a verdict string resolving | `text`, `animateOn="view"`, `sequential`, `speed` (ms per step), `className` | no `motion` import; needs only React |
| `CountUp` | PyPI and GitHub numbers | `to` (the build-time value), `separator`, `duration` (s), `format="compact"`, `holdIfInView` (default true) | server renders `to`; counts from `from` (0) when it scrolls into view after it hydrated; change `to` later and it counts on from where it is |
| `BlurText` | a section h2, never the h1 | `text`, `tag="h2"`, `className="t-h2"`, `animateBy="words"`, `delay` (ms between pieces), `blur` (px), `distance` (px), `holdIfInView` (default true) | pass it in a Section's `heading` slot |
| `RotatingText` | engine names, on the home and agents pages | `texts`, `rotationInterval` (ms), `cycles` | under reduced motion and without JS only `texts[0]` shows: print the whole list as visible text next to it. Pass `cycles={1}`: it turns through the list once and rests on `texts[0]` (M2: no loops) |

```astro
---
import DecryptedText from '../components/reactbits/DecryptedText.tsx';
import BlurText from '../components/reactbits/BlurText.tsx';
---
<p class="mono c-green"><DecryptedText client:visible text="confirmed" animateOn="view" sequential /></p>

<Section eyebrow="How it works">
  <BlurText slot="heading" client:visible text="One decision point" tag="h2" className="t-h2" delay={80} />
</Section>
```

`BlurText` and `CountUp` arm themselves when they hydrate: the heading is
hidden, the number drops to `from`. Done to text the reader is already looking
at, that reads as the page rewinding. So both hold by default
(`holdIfInView`): text that is in the viewport when the component hydrates (a
tall window, an anchor link, a restored scroll position, a slow device) stays as
the server rendered it and never plays. Hydrate them just before they scroll in,
so they arm off screen and play as they arrive:

```astro
<BlurText slot="heading" client:visible={{ rootMargin: '200px' }} text="One decision point"
  tag="h2" className="t-h2" delay={70} blur={6} distance={12} />
```

Pass `holdIfInView={false}` only for upstream's behaviour. `DecryptedText` with
`animateOn="view"` has no such hold: drive it from an island that decides when
it plays, as `RunPanel` does.

The heading text is in the server HTML twice: once visually hidden for screen
readers, once as the `aria-hidden` words that animate. `RotatingText` starts its
one turn from the name the server rendered, and clips to its own line box, so a
word on its way out never shows outside the line. It changes width as
it turns: give its wrapper `min-width` in `ch` for the longest text and keep it
with its flag in a `white-space: nowrap` span, so the line never re-wraps.

### JavaScript budget

The home page may load at most **160 KB** of JavaScript (gzip); `npm run check`
fails above it. Other pages have no check, but ship no React unless they carry
an island. Measured on this toolchain, per page:

| What the page loads | gzip |
|---|---|
| no island (the shared script and nothing else) | about 1.3 KB |
| React runtime and the hydration script, for the first island of any kind | about 71 KB |
| `DecryptedText` | + 4 KB |
| `CountUp` (pulls motion's `animate` and `useInView`) | + 31 KB |
| anything that imports the `motion` component from `motion/react` (`BlurText`, `RotatingText`, your own `motion.div`) | + 47 KB or more |

`motion` is paid for once per page: its code lands in shared chunks, so after
the first motion-based component each further one costs only its own code
(3 to 5 KB). The home page loads all of it and measures **143 KB**:

| The home page loads | gzip |
|---|---|
| React runtime and hydration | 68 KB |
| `motion` (shared by every component below) | 54 KB |
| `RunPanel`, `DecryptedText`, `BlurText`, `CountUp`, `RotatingText` | 18 KB |
| inline scripts (island hydration and props) | 3 KB |

That leaves about 17 KB. The server HTML is the final state (M3), so none of
this JavaScript blocks reading.

## Markdown

Astro 7 renders markdown with Sätteri, not remark. The link rewriter is a
Sätteri mdast plugin (`src/plugins/markdown.mjs`) around the tested rules in
`src/lib/links.mjs`:

- `x.md` → `/docs/x.html`; `adr/0050-….md` → `/docs/adr/0050-….html`; the `#hash` is kept;
- `assets/…` → `/assets/…`;
- anything that leaves `docs/` (`../SKILL.md`, `../CONTRIBUTING.md`) → the GitHub blob URL.

Ids are the lower-cased path without `.md` (`README.md` → `readme`,
`adr/README.md` → `adr/readme`). Smart punctuation is off, so `--flag` and
quotes render as GitHub renders them. The build copies `docs/**/*.md` and
`docs/assets/` into `dist/`, so `/quickstart.md` and `/assets/skep-cover.png` resolve.

## Deploy

`.github/workflows/site.yml` builds, tests and checks on every pull request,
and on every push to `main` (no path filter: a release changes only
`pyproject.toml` and `worker_contract/version.py`) builds with
`withastro/action@v6` and deploys with `actions/deploy-pages@v5`. The Pages
source must be "GitHub Actions" in the repository settings; the custom domain
is set there too, not in a `CNAME` file.

## Third-party code

`src/components/reactbits/` is from React Bits (DavidHDev/react-bits, © David
Haz), MIT + Commons Clause: it may be used in this website and may not be sold,
sublicensed or redistributed as components. It is not covered by this
repository's MIT licence (`NOTICE`), and the sdist and the container image leave `site/` out.
