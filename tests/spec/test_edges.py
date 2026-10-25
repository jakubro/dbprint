"""SPEC 2.7.2: a verdict binds on every edge but a declared one, and an absent edge's binds too."""

from __future__ import annotations

from typing import Any

import pytest

from dbprint.spec.edges import edge_key, rejected_edges, verdict_binds


def _edge(column: str, target: str, **extra: Any) -> dict[str, Any]:
    return {"column": [column], "target_table": target, "target_column": ["id"], **extra}


@pytest.mark.parametrize(
    ("edge", "binds"),
    [
        (_edge("a", "t", detection="declared"), False),
        (_edge("a", "t", detection="inferred"), True),
        (_edge("a", "t", detection="measured"), True),
        (_edge("a", "t"), True),
        (None, True),
    ],
)
def test_a_verdict_binds_everywhere_but_on_a_declared_edge(
    edge: dict[str, Any] | None,
    binds: bool,
) -> None:
    assert verdict_binds(edge) is binds


def test_only_the_rejections_that_bind_are_returned() -> None:
    declared = _edge("garden_id", "s.garden", detection="declared")
    inferred = _edge("herbarium_id", "s.herbarium", detection="inferred")
    annotated = [
        {**_edge("garden_id", "s.garden"), "verdict": "rejected", "note": "a bed code"},
        {**_edge("herbarium_id", "s.herbarium"), "verdict": "rejected"},
        {**_edge("lost_id", "s.lost"), "verdict": "rejected"},
        {**_edge("plot_id", "s.plot"), "note": "no verdict"},
        "not an entry",
    ]
    edges = {edge_key(e): e for e in (declared, inferred)}

    assert set(rejected_edges(annotated, edges)) == {
        (("herbarium_id",), "s.herbarium", ("id",)),
        (("lost_id",), "s.lost", ("id",)),
    }
