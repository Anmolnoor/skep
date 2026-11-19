import { defineCollection } from 'astro:content';
import { glob } from 'astro/loaders';
import { docId } from './lib/slugs.mjs';

// Every markdown file under ../docs (incl. adr/ and releases/). The files have no
// frontmatter, so there is no schema: the title is the first `# ` heading.
const docs = defineCollection({
  loader: glob({
    pattern: '**/*.md',
    base: '../docs',
    // One id rule for the collection, the link rewriter and the redirect shim.
    generateId: ({ entry }) => docId(entry),
  }),
});

export const collections = { docs };
