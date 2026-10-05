"""`public.order_line`: a mock table whose array, document and map columns state their parts.

The parts are candidates a descent would find; the engine's own walk decides which are kept.
"""

from __future__ import annotations

from dbprint.adapters import ColumnStats, Length, ValueCount
from dbprint.adapters.base import Distribution, Frequencies, PartStats, Range
from dbprint.adapters.mock import MockParts
from tests._prints import columns, exact_stats, mock_table, unmeasured_stats


ROWS = 100

ORDER_LINE_COLUMNS = columns(
    ("line_id", "integer"),
    ("items", "STRUCT(sku VARCHAR, qty INTEGER)[]"),
    ("attrs", "jsonb", True),
    ("labels", "MAP(VARCHAR, VARCHAR)"),
)


def order_line(**parts: MockParts) -> dict[str, object]:
    """The table, with `parts` replacing the default candidates column by column."""

    stated = {"items": items_parts(), "attrs": attrs_parts(), "labels": labels_parts(), **parts}

    return {
        "public.order_line": mock_table(
            "public.order_line",
            ORDER_LINE_COLUMNS,
            {
                "line_id": exact_stats(
                    "integer",
                    ROWS,
                    1.0,
                    distribution="uniform",
                    frequencies=Frequencies(top=1, bottom=1, listed=20, total=ROWS),
                    range=Range(min=1, max=ROWS),
                    percentiles={"p01": 1, "p25": 25, "p50": 50, "p75": 75, "p99": 99},
                    values=tuple(
                        ValueCount(value=i, count=1) for i in sorted(range(1, 21), key=str)
                    ),
                    mean=50.5,
                    sum=5050,
                    zero_count=0,
                    negative_count=0,
                    quantized_count=ROWS,
                ),
                "items": unmeasured_stats("STRUCT(sku VARCHAR, qty INTEGER)[]"),
                "attrs": exact_stats("jsonb", 40, 0.4, nullable=True),
                "labels": unmeasured_stats("MAP(VARCHAR, VARCHAR)"),
            },
            primary_key=("line_id",),
            row_count=ROWS,
            parts=stated,
        ),
    }


def items_parts() -> MockParts:
    """An array of records: the elements, and each element's two members."""

    return MockParts(
        candidates=(
            PartStats(
                path="[*]",
                occurrences=250,
                stats=_unmeasured("STRUCT(sku VARCHAR, qty INTEGER)"),
            ),
            PartStats(
                path="[*].sku",
                occurrences=250,
                stats=_categorical(
                    "VARCHAR",
                    (("SKU-A", 100), ("SKU-B", 80), ("SKU-C", 70)),
                    length=Length(min=5, max=5, avg=5.0, p95=5.0),
                ),
                samples=("SKU-A", "SKU-B", "SKU-C"),
            ),
            PartStats(
                path="[*].qty",
                occurrences=250,
                stats=ColumnStats(
                    sql_type="INTEGER",
                    nullable=True,
                    null_count=0,
                    null_rate=0.0,
                    cardinality=60,
                    cardinality_ratio=0.0,
                    cardinality_method="exact",
                    distribution="uniform",
                    frequencies=Frequencies(top=5, bottom=4, listed=20, total=250),
                    range=Range(min=1, max=60),
                    percentiles={"p01": 1, "p25": 15, "p50": 30, "p75": 45, "p99": 60},
                    values=tuple(
                        ValueCount(value=i, count=5) for i in sorted(range(1, 21), key=str)
                    ),
                    mean=30.5,
                    sum=7625,
                    zero_count=0,
                    negative_count=0,
                    quantized_count=250,
                ),
            ),
        ),
        size=Length(min=1, max=5, avg=2.5, p95=5.0),
    )


def attrs_parts() -> MockParts:
    """A document: one plainly named key, and one whose name needs quoting."""

    return MockParts(
        candidates=(
            PartStats(
                path=".status",
                occurrences=90,
                stats=_categorical(
                    "VARCHAR",
                    (("shipped", 60), ("pending", 30)),
                    length=Length(min=7, max=7, avg=7.0, p95=7.0),
                ),
                samples=("shipped", "pending"),
            ),
            PartStats(
                path='["user-id"]',
                occurrences=40,
                stats=_categorical(
                    "VARCHAR",
                    (("u1", 20), ("u2", 20)),
                    length=Length(min=2, max=2, avg=2.0, p95=2.0),
                ),
            ),
        ),
        size=Length(min=1, max=2, avg=1.3, p95=2.0),
    )


def attrs_with_contact() -> MockParts:
    """The document, plus a key whose sampled values are email addresses."""

    emails = tuple(f"grower{i}@example.invalid" for i in range(4))

    return MockParts(
        candidates=(
            *attrs_parts().candidates,
            PartStats(
                path=".contact",
                occurrences=40,
                stats=_categorical(
                    "VARCHAR",
                    tuple((email, 10) for email in emails),
                    length=Length(min=24, max=24, avg=24.0, p95=24.0),
                ),
                samples=emails,
            ),
        ),
        size=Length(min=1, max=3, avg=1.7, p95=3.0),
    )


def labels_parts() -> MockParts:
    """A map: its key set, and one key's value."""

    return MockParts(
        candidates=(
            PartStats(
                path="[keys]",
                occurrences=150,
                stats=_categorical(
                    "VARCHAR",
                    (("unit", 100), ("origin", 50)),
                    length=Length(min=4, max=6, avg=4.7, p95=6.0),
                ),
            ),
            PartStats(
                path=".unit",
                occurrences=100,
                stats=_categorical(
                    "VARCHAR",
                    (("kg", 70), ("g", 30)),
                    length=Length(min=1, max=2, avg=1.7, p95=2.0),
                    distribution="imbalanced",
                ),
            ),
        ),
        size=Length(min=1, max=2, avg=1.5, p95=2.0),
    )


def many_parts(n: int) -> MockParts:
    """`n` top-level document keys, each a two-value categorical, in descending occurrences."""

    return MockParts(
        candidates=tuple(
            PartStats(
                path=f".k{i:02d}",
                occurrences=90 - i,
                stats=_categorical(
                    "VARCHAR",
                    (("a", 45), ("b", 45 - i)),
                    length=Length(min=1, max=1, avg=1.0, p95=1.0),
                ),
            )
            for i in range(n)
        ),
    )


def _categorical(
    sql_type: str,
    counts: tuple[tuple[str, int], ...],
    *,
    length: Length,
    distribution: Distribution = "uniform",
) -> ColumnStats:
    return ColumnStats(
        sql_type=sql_type,
        nullable=True,
        null_count=0,
        null_rate=0.0,
        cardinality=len(counts),
        cardinality_ratio=0.0,
        cardinality_method="exact",
        values=tuple(ValueCount(value=v, count=c) for v, c in counts),
        values_coverage=1.0,
        distribution=distribution,
        length=length,
    )


def _unmeasured(sql_type: str) -> ColumnStats:
    return ColumnStats(
        sql_type=sql_type,
        nullable=True,
        null_count=0,
        null_rate=0.0,
        cardinality=None,
        cardinality_ratio=None,
        cardinality_method=None,
    )
