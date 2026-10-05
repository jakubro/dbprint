"""An engine's geometry-type name reaches SPEC 2.2.4's spelling, whatever the engine calls it."""

from __future__ import annotations

import pytest

from dbprint.spec.spatial import ogc_kind


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("ST_Point", "point"),
        ("POLYGONZ", "polygon"),
        ("LinearRing", "linestring"),
        ("GEOMCOLLECTION", "geometrycollection"),
        ("GEOMETRYCOLLECTION", "geometrycollection"),
    ],
)
def test_a_kind_is_spelled_as_the_spec_names_it(raw: str, expected: str) -> None:
    assert ogc_kind(raw) == expected
