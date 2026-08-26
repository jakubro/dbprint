"""Build a Mermaid relationship diagram for one table.

Pure string assembly - no Flask, no I/O. One diagram per table keeps each one inside
Mermaid's `maxTextSize`, which a connection-wide graph can exceed.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from . import catalogue


def build(name: str, relationships: dict[str, Any] | None, conn_name: str) -> str | None:
    """A `flowchart LR` of this table's FK relationships, nested by dotted-name prefix."""

    if not relationships:
        return None

    refers_to = relationships.get("refers_to") or []
    referenced_by = relationships.get("referenced_by") or []

    if not refers_to and not referenced_by:
        return None

    tables = (
        {name}
        | {r["target_table"] for r in refers_to}
        | {r["referencer_table"] for r in referenced_by}
    )
    node_ids = {t: f"n{i}" for i, t in enumerate(sorted(tables))}

    lines = ["flowchart LR"]
    _emit_subgraphs(lines, catalogue.prefix_tree(sorted(tables)), node_ids, counter=[0])

    for r in refers_to:
        arrow = "-.->|" if r.get("detection") == "inferred" else "-->|"
        cols = ", ".join(r.get("column") or [])
        lines.append(f'  {node_ids[name]} {arrow}"{cols}"| {node_ids[r["target_table"]]}')

    for r in referenced_by:
        arrow = "-.->|" if r.get("detection") == "inferred" else "-->|"
        cols = ", ".join(r.get("referencer_column") or [])
        lines.append(f'  {node_ids[r["referencer_table"]]} {arrow}"{cols}"| {node_ids[name]}')

    for t in sorted(tables):
        lines.append(f'  click {node_ids[t]} "/t/{conn_name}/{quote(t)}" "{t}"')

    lines.append("  classDef current fill:#4a90d922,stroke:#4a90d9,stroke-width:2px;")
    lines.append(f"  class {node_ids[name]} current;")

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
