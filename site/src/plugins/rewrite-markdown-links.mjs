import path from "node:path";

import { visit } from "unist-util-visit";

const PLUGIN_SKILL_RE = /^\.\.\/plugin\/skills\/([^/]+)\/SKILL\.md$/;

export function publishedRoute(docsRelative) {
  const skill = PLUGIN_SKILL_RE.exec(docsRelative);

  if (skill) {
    return `examples/skill/${skill[1]}`;
  }

  return docsRelative.startsWith("..") ? null : docsRelative.replace(/\.md$/, "").toLowerCase();
}

// A relative target 404s under `base`: a .md link is rewritten to its published route, anything
// else to a repository URL. Throwing here is useless - Astro logs it and ships an empty page.
export function rewriteMarkdownLinks({ docsRoot, base, repository, ref }) {
  return (tree, file) => {
    visit(tree, "link", (link) => {
      const [target, hash] = link.url.split("#");

      if (!target || target.startsWith("http")) {
        return;
      }

      const sourceDir = path.dirname(file.path);
      const absolute = path.resolve(sourceDir, target);
      const relative = path.relative(docsRoot, absolute);
      const route = target.endsWith(".md") ? publishedRoute(relative) : null;

      if (relative.startsWith("..") && route === null) {
        throw new Error(`${file.path}: link target escapes the docs root: ${link.url}`);
      }

      const suffix = hash ? `#${hash}` : "";

      if (route !== null) {
        link.url = `${base}/${route}/${suffix}`;

        return;
      }

      // A trailing slash names which of git's two path views this is; the link check verifies it.
      const kind = target.endsWith("/") ? "tree" : "blob";

      link.url = `${repository}/${kind}/${ref}/docs/${relative}${suffix}`;
    });
  };
}
