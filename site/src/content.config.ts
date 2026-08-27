import { readFileSync } from "node:fs";

import { docsSchema } from "@astrojs/starlight/schema";
import { glob, type Loader } from "astro/loaders";
import { defineCollection } from "astro:content";

const LEADING_HEADING_RE = /^#\s+(.+)$/m;

// docsLoader() cannot be repointed off src/content/docs; glob() can. `parseData` synthesizes
// Starlight's required `title` from each file's H1 - a remark plugin runs after it, too late.
function titledDocsLoader(): Loader {
  const inner = glob({
    pattern: "**/*.md",
    base: "../docs",
    generateId: ({ entry }) => entry.replace(/\.md$/, "").toLowerCase(),
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
