import { cpSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import react from '@astrojs/react';
import { satteri } from '@astrojs/markdown-satteri';
import { defineConfig, fontProviders } from 'astro/config';
import { rewriteLinks } from './src/plugins/markdown.mjs';

// The markdown under ../docs is the content source; this file is the one place
// that knows where it is. (import.meta.url is reliable here: the config is not bundled.)
const docsDir = fileURLToPath(new URL('../docs/', import.meta.url));

/** Raw markdown URLs (/quickstart.md) and README image paths (/assets/…) keep resolving. */
function copyDocs() {
  return {
    name: 'skep-copy-docs',
    hooks: {
      'astro:build:done': ({ dir }) => {
        const out = fileURLToPath(dir);
        cpSync(docsDir, out, {
          recursive: true,
          filter: (source) => statSync(source).isDirectory() || source.endsWith('.md'),
        });
        cpSync(join(docsDir, 'assets'), join(out, 'assets'), { recursive: true });
      },
    },
  };
}

export default defineConfig({
  site: 'https://skep.anmolnoor.com',
  // No `base`. 'file' keeps every old URL: /how-it-works.html, /docs.html, …
  build: { format: 'file' },
  trailingSlash: 'never',
  // Astro 7 defaults to 'jsx', which drops the space between text and an inline
  // element on the next line ("from<a>the docs</a>"). `true` keeps HTML whitespace.
  compressHTML: true,
  integrations: [react(), copyDocs()],
  fonts: [
    {
      provider: fontProviders.fontsource(),
      name: 'Geist',
      cssVariable: '--font-geist',
      weights: ['400 700'],
      styles: ['normal'],
      subsets: ['latin'],
      fallbacks: ['ui-sans-serif', 'system-ui', 'sans-serif'],
    },
    {
      provider: fontProviders.fontsource(),
      name: 'Geist Mono',
      cssVariable: '--font-geist-mono',
      weights: ['400 600'],
      styles: ['normal'],
      subsets: ['latin'],
      fallbacks: ['ui-monospace', 'monospace'],
    },
  ],
  markdown: {
    // Astro 7 renders markdown with Sätteri; its mdast plugins replace remark plugins.
    processor: satteri({
      mdastPlugins: [rewriteLinks({ docsDir })],
      // The docs are written for GitHub's renderer, which leaves `--flag` and quotes alone.
      features: { smartPunctuation: false },
    }),
    shikiConfig: { theme: 'github-dark-default' },
  },
});
