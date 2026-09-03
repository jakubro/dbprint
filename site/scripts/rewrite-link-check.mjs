#!/usr/bin/env node
// Cross-check helper for `tests/test_packaged_specifications.py`: runs the site's own
// `rewriteMarkdownLinks` plugin over one stdin JSON line and prints the rewritten target.

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { unified } from "unified";
import remarkParse from "remark-parse";
import remarkStringify from "remark-stringify";

import { rewriteMarkdownLinks } from "../src/plugins/rewrite-markdown-links.mjs";

const DOCS_ROOT = fileURLToPath(new URL("../../docs", import.meta.url));
const SITE_ORIGIN = "https://jakubro.github.io";
const BASE = "/dbprint";
const REPOSITORY = "https://github.com/jakubro/dbprint";
const REF = "main";

const processor = unified()
  .use(remarkParse)
  .use(rewriteMarkdownLinks, {
    docsRoot: DOCS_ROOT,
    base: `${SITE_ORIGIN}${BASE}`,
    repository: REPOSITORY,
    ref: REF,
  })
  .use(remarkStringify);

const lines = readFileSync(0, "utf8").split("\n").filter(Boolean);

for (const line of lines) {
  const { text, docRelpath } = JSON.parse(line);
  const file = { path: `${DOCS_ROOT}/${docRelpath}` };
  const tree = processor.parse(text);
  processor.runSync(tree, file);
  const rewritten = processor.stringify(tree);
  const match = rewritten.match(/\(([^)]*)\)/);
  process.stdout.write(`${match ? match[1] : ""}\n`);
}
