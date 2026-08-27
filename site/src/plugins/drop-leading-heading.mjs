// The synthesized Starlight title (src/content.config.ts) duplicates each
// page's own leading H1; this removes it once Starlight owns the heading.
export function dropLeadingHeading() {
  return (tree) => {
    const first = tree.children[0];

    if (first?.type === "heading" && first.depth === 1) {
      tree.children.shift();
    }
  };
}
