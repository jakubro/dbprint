#!/usr/bin/env node
// Run `astro build`, failing it when it prints a warning; KNOWN drops what the site has no switch for.

import { spawn } from "node:child_process";
import { createInterface } from "node:readline";

// Astro colours its log prefixes whenever `CI` is set, piped or not.
const ANSI_RE = /\x1b\[[0-9;]*[A-Za-z]/g;
const WARNING_RE = /\b(?:warn(?:ing|ings)?|deprecated)\b|[a-z]Warning\b/i;
const KNOWN = [
  [
    /(?:\d\d:\d\d:\d\d )?\[WARN\] \[content\] The collection "i18n" does not exist or is empty\. Please check your content config file for errors\./,
    "Starlight reads an optional translations collection; the site defines none",
  ],
  [
    /(?:\d\d:\d\d:\d\d )?\[WARN\] \[content\] Entry docs → 404 was not found\./,
    "Starlight reads an optional custom 404 entry, then renders its own page",
  ],
];

const build = spawn("astro", ["build"], { stdio: ["inherit", "pipe", "pipe"] });
const warnings = [];

for (const stream of [build.stdout, build.stderr]) {
  createInterface({ input: stream }).on("line", (raw) => {
    const plain = raw.replace(ANSI_RE, "");
    const line = KNOWN.reduce((text, [pattern]) => text.replace(pattern, ""), plain);

    if (line.trim() === "" && plain.trim() !== "") {
      return;
    }

    console.log(line === plain ? raw : line);

    if (WARNING_RE.test(line)) {
      warnings.push(line.trim());
    }
  });
}

build.on("close", (code) => {
  if (warnings.length > 0) {
    console.error(`\n${warnings.length} warning line(s); the site build prints none:`);
    warnings.forEach((line) => console.error(`  ${line}`));
  }

  // A build killed by a signal closes with no code.
  process.exit(code === null ? 1 : code !== 0 ? code : warnings.length > 0 ? 1 : 0);
});
