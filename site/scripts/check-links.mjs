#!/usr/bin/env node
// Postbuild link/anchor check: astro build cannot see a target page's headings while processing
// the source that links to it, so every href, fragment and repository path resolves against dist/.

import { readFileSync, readdirSync, statSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const BASE = "/dbprint";
const DIST = fileURLToPath(new URL("../dist", import.meta.url));
const DOCS = fileURLToPath(new URL("../../docs", import.meta.url));

// Kept in step with astro.config.mjs by hand, as BASE above already is.
const REPOSITORY = "https://github.com/jakubro/dbprint";
const REF = "main";

const HREF_RE = /href="([^"]+)"/g;
const ID_RE = /\sid="([^"]+)"/g;
const REPO_DOCS_RE = /^(tree|blob)\/([^/]+)\/docs\/(.+)$/;

function allFiles(dir) {
  const found = [];

  for (const name of readdirSync(dir)) {
    const full = path.join(dir, name);

    if (statSync(full).isDirectory()) {
      found.push(...allFiles(full));
    } else {
      found.push(full);
    }
  }

  return found;
}

function routeFor(file) {
  return `${BASE}/${path.relative(DIST, file).replace(/\\/g, "/")}`;
}

const files = allFiles(DIST);
const filePaths = new Set(files.map(routeFor));
const htmlFiles = files.filter((f) => f.endsWith(".html"));
const idsByPage = new Map();

for (const file of htmlFiles) {
  const html = readFileSync(file, "utf-8");
  const ids = new Set([...html.matchAll(ID_RE)].map((m) => m[1]));

  idsByPage.set(routeFor(file), ids);
}

// The `docs/`-relative path a repository href names, or null when it is not one of ours.
function repoDocsTarget(href) {
  if (!href.startsWith(`${REPOSITORY}/`)) return null;

  const match = REPO_DOCS_RE.exec(href.slice(REPOSITORY.length + 1));

  return match && match[2] === REF ? { kind: match[1], relative: match[3] } : null;
}

function resolves(hrefPath) {
  if (filePaths.has(hrefPath)) return hrefPath;

  const indexed = hrefPath.endsWith("/") ? `${hrefPath}index.html` : `${hrefPath}/index.html`;

  return filePaths.has(indexed) ? indexed : null;
}

const failures = [];

for (const file of htmlFiles) {
  const html = readFileSync(file, "utf-8");

  for (const match of html.matchAll(HREF_RE)) {
    const href = match[1];
    const repo = repoDocsTarget(href);

    if (repo) {
      const stats = statSync(path.join(DOCS, repo.relative), { throwIfNoEntry: false });

      if (!stats) {
        failures.push(`${file}: repository link names a path not in docs/: ${href}`);
      } else if (stats.isDirectory() !== (repo.kind === "tree")) {
        const actual = stats.isDirectory() ? "directory" : "file";

        failures.push(`${file}: repository link says ${repo.kind} for a ${actual}: ${href}`);
      }

      continue;
    }

    if (!href.startsWith(BASE)) continue;

    const [hrefPath, hash] = href.split("#");

    if (hrefPath.endsWith(".md")) {
      failures.push(`${file}: href ends in .md: ${href}`);
      continue;
    }

    const resolved = resolves(hrefPath);

    if (!resolved) {
      failures.push(`${file}: dangling link target: ${href}`);
      continue;
    }

    if (hash && resolved.endsWith(".html") && !idsByPage.get(resolved)?.has(hash)) {
      failures.push(`${file}: dangling anchor #${hash} on ${resolved}`);
    }
  }
}

if (failures.length > 0) {
  console.error(`${failures.length} link check failure(s):`);
  failures.forEach((f) => console.error(`  ${f}`));
  process.exit(1);
}

console.log(`link check clean: ${htmlFiles.length} page(s), zero dangling targets`);
