"""The `geometry` and `extent` fields of a `spatial` column per SPEC 2.2.4, folded from groups.

Pure: an adapter reads one grouped statement per column and hands its rows here.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any

from .rounding import DECIMAL_PLACES


Dimensions = str
Bound = float | Decimal

_OGC_KINDS = {
    "linearring": "linestring",
    "ring": "linestring",
    "geomcollection": "geometrycollection",
}
_DIMENSION_FLAGS: dict[int, Dimensions] = {0: "xy", 1: "xym", 2: "xyz", 3: "xyzm"}


@dataclass(frozen=True)
class SpatialGroup:
    """One group of a column's non-null values sharing a kind, reference system and dimensions."""

    kind: str
    srid: int | str
    dimensions: Dimensions
    count: int
    empty: int
    invalid: int
    min_x: Bound | None = None
    min_y: Bound | None = None
    max_x: Bound | None = None
    max_y: Bound | None = None


@dataclass(frozen=True)
class Geometry:
    """SPEC 2.2.4's `geometry`: each list ordered by count descending, then by key."""

    kinds: tuple[tuple[str, int], ...]
    srids: tuple[tuple[int | str, int], ...] | None
    dimensions: tuple[tuple[Dimensions, int], ...]
    empty_count: int
    invalid_count: int | None


@dataclass(frozen=True)
class Extent:
    """SPEC 2.2.4's `extent`, X and Y only, each bound rounded outward."""

    min_x: float
    min_y: float
    max_x: float
    max_y: float


def ogc_kind(raw: str) -> str:
    """An engine's geometry-type name as SPEC 2.2.4 spells it: lowercase, no `ST_`, no Z/M."""

    name = raw.strip().lower().removeprefix("st_")

    for suffix in ("zm", "z", "m"):
        if name.endswith(suffix) and name[: -len(suffix)] in _KNOWN_KINDS:
            name = name[: -len(suffix)]

            break

    return _OGC_KINDS.get(name, name)


def dimensions_of(flag: int) -> Dimensions:
    """PostGIS's `ST_Zmflag` reading (0 xy, 1 xym, 2 xyz, 3 xyzm) as a `dimensions` value."""

    return _DIMENSION_FLAGS[flag]


def fold(
    groups: list[SpatialGroup],
    *,
    with_srids: bool = True,
    with_validity: bool = True,
) -> tuple[Geometry, Extent | None]:
    """The column's `geometry` and `extent` from its groups; no `extent` without a bounded value.

    `with_srids`/`with_validity` are False on an engine whose type carries no reference system,
    or can hold no invalid value (SPEC 2.2.4).
    """

    kinds: Counter[str] = Counter()
    srids: Counter[int | str] = Counter()
    dimensions: Counter[Dimensions] = Counter()

    for group in groups:
        kinds[group.kind] += group.count
        srids[group.srid] += group.count
        dimensions[group.dimensions] += group.count

    geometry = Geometry(
        kinds=_ordered(kinds),
        srids=_ordered(srids) if with_srids else None,
        dimensions=_ordered(dimensions),
        empty_count=sum(g.empty for g in groups),
        invalid_count=sum(g.invalid for g in groups) if with_validity else None,
    )
    bounds = [g for g in groups if None not in (g.min_x, g.min_y, g.max_x, g.max_y)]

    if not bounds:
        return geometry, None

    extent = Extent(
        min_x=outward(min(g.min_x for g in bounds if g.min_x is not None), ROUND_FLOOR),
        min_y=outward(min(g.min_y for g in bounds if g.min_y is not None), ROUND_FLOOR),
        max_x=outward(max(g.max_x for g in bounds if g.max_x is not None), ROUND_CEILING),
        max_y=outward(max(g.max_y for g in bounds if g.max_y is not None), ROUND_CEILING),
    )

    return geometry, extent


def outward(value: Bound, rounding: str) -> float:
    """`value` at six decimals, floored or ceiled so a bounding box still contains its values."""

    exact = value if isinstance(value, Decimal) else Decimal(repr(value))

    if not exact.is_finite():
        return float(exact)

    return float(exact.quantize(Decimal(1).scaleb(-DECIMAL_PLACES), rounding=rounding))


_KNOWN_KINDS = frozenset(
    {
        "point",
        "linestring",
        "polygon",
        "multipoint",
        "multilinestring",
        "multipolygon",
        "geometrycollection",
        "circularstring",
        "compoundcurve",
        "curvepolygon",
        "multicurve",
        "multisurface",
        "polyhedralsurface",
        "triangle",
        "tin",
        "linearring",
        "ring",
    },
)


def _ordered(counts: Counter[Any]) -> tuple[tuple[Any, int], ...]:
    return tuple(sorted(counts.items(), key=lambda item: (-item[1], str(item[0]))))
