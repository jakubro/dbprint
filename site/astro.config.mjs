import { fileURLToPath } from "node:url";

import starlight from "@astrojs/starlight";
import { defineConfig } from "astro/config";

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
      { label: "PostgreSQL", slug: "adapters/postgres" },
      { label: "MySQL", slug: "adapters/mysql" },
      { label: "Snowflake", slug: "adapters/snowflake" },
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
    starlight({
      title: "dbprint",
      sidebar: SIDEBAR,
      customCss: ["./src/styles/custom.css"],
    }),
  ],
});
