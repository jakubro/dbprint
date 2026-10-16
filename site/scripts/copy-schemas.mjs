#!/usr/bin/env node
// Copies the packaged JSON Schemas into public/ so `astro build` serves each at its own `$id`
// path. Gitignored - a committed copy would fork the normative source.

import { mkdirSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

const SOURCE = fileURLToPath(new URL("../../src/dbprint/spec/v1", import.meta.url));
const TARGET = fileURLToPath(new URL("../public/spec/v1", import.meta.url));

mkdirSync(TARGET, { recursive: true });

const schemas = readdirSync(SOURCE).filter((name) => name.endsWith(".schema.json"));

for (const name of schemas) {
  writeFileSync(`${TARGET}/${name}`, readFileSync(`${SOURCE}/${name}`));
}

const CONFIG_SOURCE = fileURLToPath(
  new URL("../../src/dbprint/config/project.schema.json", import.meta.url),
);
const CONFIG_TARGET = fileURLToPath(new URL("../public/config", import.meta.url));

mkdirSync(CONFIG_TARGET, { recursive: true });
writeFileSync(`${CONFIG_TARGET}/project.schema.json`, readFileSync(CONFIG_SOURCE));

console.log(`copied ${schemas.length} schema(s) to public/spec/v1/ and the config schema to public/config/`);
