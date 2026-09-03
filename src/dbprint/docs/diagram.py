"""Build a Mermaid relationship diagram for one table.

Pure string assembly - no Flask, no I/O. One diagram per table keeps each one inside
Mermaid's `maxTextSize`, which a connection-wide graph can exceed.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from . import catalogue


# Solid for a constraint the database enforces, dotted for a name that suggests one, thick for
# a measured containment - the only place `detection` shows, the edge label carrying columns.
_ARROW_BY_DETECTION = {"declared": "-->|", "inferred": "-.->|", "measured": "==>|"}

# `depends_on` (SPEC 2.2.17) is a different relation - object grain, not a foreign key - so it
# gets an open-circle head rather than reading as a fourth detection value.
_DEPENDS_ON_ARROW = "--o|"


def build(
    name: str,
    rows: dict[str, Any] | None,
    conn_name: str,
    depends_on: tuple[str, ...] = (),
) -> str | None:
    """A `flowchart LR` of this table's FK relationships plus what it reads (SPEC 2.2.17) -
    `rows` arrives normalized, and `depends_on` renders with its own arrow shape.
    """

    refers_to = (rows or {}).get("refers_to") or []
    referenced_by = (rows or {}).get("referenced_by") or []

    if not refers_to and not referenced_by and not depends_on:
        return None

    tables = (
        {name}
        | {r["target_table"] for r in refers_to}
        | {r["referencer_table"] for r in referenced_by}
        | set(depends_on)
    )
    node_ids = {t: f"n{i}" for i, t in enumerate(sorted(tables))}

    lines = ["flowchart LR"]
    _emit_subgraphs(lines, catalogue.prefix_tree(sorted(tables)), node_ids, counter=[0])

    rejected_links: list[int] = []
    depends_on_links: list[int] = []
    link_index = 0

    for r in refers_to:
        arrow = _ARROW_BY_DETECTION.get(r.get("detection"), "-.->|")
        cols = ", ".join(r.get("column") or [])
        lines.append(f'  {node_ids[name]} {arrow}"{cols}"| {node_ids[r["target_table"]]}')

        if r.get("rejected"):
            rejected_links.append(link_index)

        link_index += 1

    for r in referenced_by:
        arrow = _ARROW_BY_DETECTION.get(r.get("detection"), "-.->|")
        cols = ", ".join(r.get("referencer_column") or [])
        lines.append(f'  {node_ids[r["referencer_table"]]} {arrow}"{cols}"| {node_ids[name]}')

        if r.get("rejected"):
            rejected_links.append(link_index)

        link_index += 1

    for target in depends_on:
        lines.append(f'  {node_ids[name]} {_DEPENDS_ON_ARROW}"reads"| {node_ids[target]}')
        depends_on_links.append(link_index)
        link_index += 1

    for t in sorted(tables):
        lines.append(f'  click {node_ids[t]} "/t/{conn_name}/{quote(t)}" "{t}"')

    lines.append("  classDef current fill:#4a90d922,stroke:#4a90d9,stroke-width:2px;")
    lines.append(f"  class {node_ids[name]} current;")

    # A rejected edge stays on the diagram (it is still a real, if disputed, edge) but reads
    # visibly muted - the same fact the badge beside the relationship table already states.
    for idx in rejected_links:
        lines.append(f"  linkStyle {idx} stroke:#c00,stroke-width:1px,opacity:0.5;")

    # Muted gray, on top of the open-circle shape above - two independent signals so a
    # depends_on edge is never mistaken for a foreign key even in a monochrome render.
    for idx in depends_on_links:
        lines.append(f"  linkStyle {idx} stroke:#888,stroke-dasharray:3 3;")

    return "\n".join(lines)


def _emit_subgraphs(
    lines: list[str],
    tree: catalogue.PrefixTree,
    node_ids: dict[str, str],
    counter: list[int],
) -> None:
    """Recursively wrap a prefix tree's groups in nested Mermaid subgraph blocks."""

    for t in tree.leaves:
        lines.append(f'  {node_ids[t]}["{t.split(".")[-1]}"]')

    for group_name, subtree in tree.groups.items():
        counter[0] += 1
        lines.append(f'  subgraph sg{counter[0]}["{group_name}"]')
        _emit_subgraphs(lines, subtree, node_ids, counter)
        lines.append("  end")
