"""`public.fieldwork`: a mock table whose array, document and map columns state their parts.

The parts are candidates a descent would find; the engine's own walk decides which are kept.
"""

from __future__ import annotations

from dbprint.adapters import ColumnStats, Length, ValueCount
from dbprint.adapters.base import Distribution, Frequencies, PartStats, Range
from dbprint.adapters.mock import MockParts
from dbprint.spec.parts import member
from tests._prints import columns, exact_stats, mock_table, unmeasured_stats


ROWS = 100

FIELDWORK_COLUMNS = columns(
    ("fieldwork_id", "integer"),
    ("items", "STRUCT(lot VARCHAR, seeds INTEGER)[]"),
    ("attrs", "jsonb", True),
    ("labels", "MAP(VARCHAR, VARCHAR)"),
)


def fieldwork(**parts: MockParts) -> dict[str, object]:
    """The table, with `parts` replacing the default candidates column by column."""

    stated = {"items": items_parts(), "attrs": attrs_parts(), "labels": labels_parts(), **parts}

    return {
        "public.fieldwork": mock_table(
            "public.fieldwork",
            FIELDWORK_COLUMNS,
            {
                "fieldwork_id": exact_stats(
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
                "items": unmeasured_stats("STRUCT(lot VARCHAR, seeds INTEGER)[]"),
                "attrs": exact_stats("jsonb", 40, 0.4, nullable=True),
                "labels": unmeasured_stats("MAP(VARCHAR, VARCHAR)"),
            },
            primary_key=("fieldwork_id",),
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
                stats=_unmeasured("STRUCT(lot VARCHAR, seeds INTEGER)"),
            ),
            PartStats(
                path="[*].lot",
                occurrences=250,
                stats=_categorical(
                    "VARCHAR",
                    (("LOT-A", 100), ("LOT-B", 80), ("LOT-C", 70)),
                    length=Length(min=5, max=5, avg=5.0, p95=5.0),
                ),
                samples=("LOT-A", "LOT-B", "LOT-C"),
            ),
            PartStats(
                path="[*].seeds",
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
                    (("planted", 60), ("pending", 30)),
                    length=Length(min=7, max=7, avg=7.0, p95=7.0),
                ),
                samples=("planted", "pending"),
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


def attrs_with_mostly_contact() -> MockParts:
    """The document, plus a key whose sampled values are nine addresses and a placeholder."""

    values = (*(f"grower{i}@example.invalid" for i in range(9)), "n/a")

    return MockParts(
        candidates=(
            *attrs_parts().candidates,
            PartStats(
                path=".relay",
                occurrences=40,
                stats=_categorical(
                    "VARCHAR",
                    tuple((value, 4) for value in values),
                    length=Length(min=3, max=23, avg=21.0, p95=23.0),
                ),
                samples=values,
            ),
        ),
        size=Length(min=1, max=3, avg=1.7, p95=3.0),
    )


def attrs_with_vault_address() -> MockParts:
    """The document, plus a `vault_address` object whose members spell street-address keys."""

    streets = tuple(f"{i} Quietleak Road" for i in range(1, 5))

    return MockParts(
        candidates=(
            *attrs_parts().candidates,
            PartStats(path=".vault_address", occurrences=80, stats=_unmeasured("JSON")),
            PartStats(
                path=".vault_address[keys]",
                occurrences=160,
                stats=_categorical(
                    "VARCHAR",
                    (("line1", 80), ("city", 80)),
                    length=Length(min=4, max=5, avg=4.5, p95=5.0),
                ),
                samples=("line1", "city"),
            ),
            PartStats(
                path=".vault_address.line1",
                occurrences=80,
                stats=_categorical(
                    "VARCHAR",
                    tuple((street, 20) for street in streets),
                    length=Length(min=15, max=15, avg=15.0, p95=15.0),
                ),
                samples=streets,
                keyed=True,
            ),
            PartStats(
                path=".vault_address.city",
                occurrences=80,
                stats=_categorical(
                    "VARCHAR",
                    (("Leakton", 50), ("Townleak", 30)),
                    length=Length(min=7, max=8, avg=7.4, p95=8.0),
                ),
                samples=("Leakton", "Townleak"),
                keyed=True,
            ),
        ),
        size=Length(min=1, max=3, avg=2.0, p95=3.0),
    )


def labels_keyed_by_email() -> MockParts:
    """A map keyed by email addresses, two keyed members and a part below the first of them."""

    emails = ("grower0@example.invalid", "grower1@example.invalid")
    first = member(emails[0])

    return MockParts(
        candidates=(
            PartStats(
                path="[keys]",
                occurrences=200,
                stats=_categorical(
                    "VARCHAR",
                    tuple((email, 100) for email in emails),
                    length=Length(min=23, max=23, avg=23.0, p95=23.0),
                ),
                samples=emails,
            ),
            *(
                PartStats(
                    path=member(email),
                    occurrences=100,
                    stats=_categorical("VARCHAR", (("kg", 100),), length=Length(2, 2, 2.0, 2.0)),
                    samples=("kg",),
                    keyed=True,
                )
                for email in emails
            ),
            PartStats(
                path=f"{first}.unit",
                occurrences=100,
                stats=_categorical("VARCHAR", (("kg", 100),), length=Length(2, 2, 2.0, 2.0)),
                samples=("kg",),
            ),
        ),
    )


def items_with_home_address() -> MockParts:
    """An array of records each holding a `home_address` record of two text fields."""

    return MockParts(
        candidates=(
            PartStats(
                path="[*]",
                occurrences=250,
                stats=_unmeasured("STRUCT(home_address STRUCT(line1 VARCHAR, town VARCHAR))"),
            ),
            PartStats(
                path="[*].home_address",
                occurrences=250,
                stats=_unmeasured("STRUCT(line1 VARCHAR, town VARCHAR)"),
            ),
            PartStats(
                path="[*].home_address.line1",
                occurrences=250,
                stats=_categorical(
                    "VARCHAR",
                    (("7 Wisteria Lane", 150), ("9 Wisteria Lane", 100)),
                    length=Length(min=15, max=15, avg=15.0, p95=15.0),
                ),
                samples=("7 Wisteria Lane", "9 Wisteria Lane"),
            ),
            PartStats(
                path="[*].home_address.town",
                occurrences=250,
                stats=_categorical(
                    "VARCHAR",
                    (("Townleak", 250),),
                    length=Length(min=8, max=8, avg=8.0, p95=8.0),
                ),
                samples=("Townleak",),
            ),
        ),
        size=Length(min=1, max=1, avg=1.0, p95=1.0),
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
