import { fileURLToPath } from "node:url";

import starlight from "@astrojs/starlight";
import { defineConfig } from "astro/config";
import mermaid from "astro-mermaid";
import starlightThemeRapide from "starlight-theme-rapide";

import { dropLeadingHeading } from "./src/plugins/drop-leading-heading.mjs";
import { rewriteMarkdownLinks } from "./src/plugins/rewrite-markdown-links.mjs";

const BASE = "/dbprint";
const DOCS_ROOT = fileURLToPath(new URL("../docs", import.meta.url));

// Where a link the site does not publish is sent; pinned to the published branch.
const REPOSITORY = "https://github.com/jakubro/dbprint";
const REF = "main";

// The reference example's own print root, which every one of its prose pages routes under.
const EXAMPLE = "format/v1/examples/production/prints/production";

// Ordered by what a reader needs first - a task before a schema, never alphabetically.
const SIDEBAR = [
  {
    label: "Start",
    items: [
      { label: "What dbprint is", slug: "index" },
      { label: "Your first print", slug: "start" },
    ],
  },
  {
    label: "Guides",
    items: [
      { label: "Choosing what to profile", slug: "guide/scoping" },
      { label: "Withholding cell values", slug: "guide/redaction" },
      { label: "Annotating a print", slug: "guide/annotations" },
      { label: "Tracking drift", slug: "guide/drift" },
      { label: "Gating CI", slug: "guide/ci" },
      { label: "Browsing a print", slug: "guide/browsing" },
      { label: "Giving a print to an agent", slug: "guide/agents" },
      { label: "When something goes wrong", slug: "guide/troubleshooting" },
      {
        label: "The packaged agent skill",
        collapsed: true,
        items: [
          { label: "Installing it", slug: "examples/skill/readme" },
          { label: "The skill itself", slug: "examples/skill/dbprint" },
        ],
      },
    ],
  },
  {
    label: "Adapters",
    items: [
      { label: "duckdb", slug: "adapters/duckdb" },
      { label: "PostgreSQL", slug: "adapters/postgres" },
      { label: "MySQL", slug: "adapters/mysql" },
      { label: "ClickHouse", slug: "adapters/clickhouse" },
      { label: "Redshift", slug: "adapters/redshift" },
      { label: "Snowflake", slug: "adapters/snowflake" },
      { label: "Databricks", slug: "adapters/databricks" },
      { label: "BigQuery", slug: "adapters/bigquery" },
    ],
  },
  {
    label: "Producing dbprint output",
    items: [{ label: "Emitting a conforming print", slug: "producers" }],
  },
  {
    label: "Reference",
    items: [
      { label: "Configuration", slug: "config" },
      { label: "Command line", slug: "cli" },
      { label: "Assertions", slug: "assertions" },
      { label: "MCP server", slug: "mcp" },
      { label: "Conformance codes", slug: "reference/conformance" },
      { label: "Statistics required-field matrix", slug: "reference/statistics-matrix" },
      { label: "Format specification v1", slug: "format/v1/spec" },
      {
        label: "Reference example",
        collapsed: true,
        items: [
          { label: "The example print", slug: "format/v1/examples/readme" },
          { label: "Its consumer guide", slug: `${EXAMPLE}/reading` },
          { label: "accession", slug: `${EXAMPLE}/seedbank/accession/description` },
          { label: "collector", slug: `${EXAMPLE}/seedbank/collector/description` },
          { label: "germination_trial", slug: `${EXAMPLE}/seedbank/germination_trial/description` },
          { label: "taxon", slug: `${EXAMPLE}/seedbank/taxon/description` },
          { label: "The vocabulary example", slug: "format/v1/examples/vocabulary/readme" },
          { label: "Its consumer guide", slug: "format/v1/examples/vocabulary/prints/vocabulary/reading" },
        ],
      },
    ],
  },
  {
    label: "Internal",
    collapsed: true,
    items: [
      { label: "Architecture", slug: "architecture" },
      { label: "Contributor guidelines", slug: "guidelines" },
    ],
  },
];

export default defineConfig({
  site: "https://jakubro.github.io",
  base: BASE,
  trailingSlash: "always",
  markdown: {
    smartypants: false,
    remarkPlugins: [
      dropLeadingHeading,
      [rewriteMarkdownLinks, { docsRoot: DOCS_ROOT, base: BASE, repository: REPOSITORY, ref: REF }],
    ],
  },
  integrations: [
    // Claims fenced mermaid blocks before Starlight's renderer sees them, so it must stay first.
    mermaid({ autoTheme: true }),
    starlight({
      title: "dbprint",
      plugins: [starlightThemeRapide()],
      components: {
        ThemeProvider: "./src/components/ThemeProvider.astro",
        // Adds the demo recording above the landing page prose; other routes fall through as-is.
        MarkdownContent: "./src/components/MarkdownContent.astro",
      },
      // Inlined rather than emitted: trailingSlash "always" appends a slash to the hashed
      // /_astro/ec.<hash>.css route, so the dev server never matches it and code blocks lose styling.
      expressiveCode: { emitExternalStylesheet: false },
      sidebar: SIDEBAR,
      customCss: ["./src/styles/custom.css"],
    }),
  ],
});
