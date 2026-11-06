import { readFileSync } from "node:fs";
import path from "node:path";

import { docsSchema } from "@astrojs/starlight/schema";
import { glob, type Loader } from "astro/loaders";
import { defineCollection } from "astro:content";

import { publishedRoute } from "./plugins/rewrite-markdown-links.mjs";

const LEADING_HEADING_RE = /^#\s+(.+)$/m;

// docsLoader() cannot be repointed off src/content/docs; glob() can. `parseData` synthesizes
// Starlight's required `title` from each file's H1 - a remark plugin runs after it, too late.
function titledDocsLoader(): Loader {
  const inner = glob({
    pattern: ["docs/**/*.md", "plugin/skills/*/SKILL.md"],
    base: "..",
    generateId: ({ entry }) => publishedRoute(path.posix.relative("docs", entry)),
  });

  return {
    name: "titled-docs-loader",
    load: (context) =>
      inner.load({
        ...context,
        parseData: async (props) => {
          if (!props.data.title && props.filePath) {
            const heading = readFileSync(props.filePath, "utf-8").match(LEADING_HEADING_RE);

            if (heading) {
              props.data = { ...props.data, title: heading[1].trim() };
            }
          }

          return context.parseData(props);
        },
      }),
  };
}

export const collections = {
  docs: defineCollection({ loader: titledDocsLoader(), schema: docsSchema() }),
};
