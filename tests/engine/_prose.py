"""A table mixing prose, email and short-text columns, the shape value-list suppression reads."""

from __future__ import annotations

from dbprint.adapters import (
    ColumnStats,
    Length,
    MockTable,
    ValueCount,
)
from tests._prints import columns, mock_table


PROSE = [f"the quick brown fox number {i} jumped over a lazy dog today" for i in range(40)]


EMAILS = [f"user{i}@example.com" for i in range(40)]


def prose_fixture() -> dict[str, MockTable]:
    """`field_notes` and `phone` are prose and suppressed; `institution` (text, reporting `email`)
    and `status` (categorical, reporting prose) are not reached by the exemption.
    """

    # A hundred distinct values over two hundred rows: above the enumeration threshold, so
    # the column classifies text, and the top-twenty list covers a fifth - hence long_tail.
    listed = tuple(ValueCount(value=f"v{i:02d}", count=2) for i in range(20))

    def text_stats() -> ColumnStats:
        return ColumnStats(
            sql_type="text",
            nullable=False,
            null_count=0,
            null_rate=0.0,
            cardinality=100,
            cardinality_ratio=0.5,
            cardinality_method="exact",
            values=listed,
            values_coverage=0.2,
            distribution="long_tail",
            empty_count=0,
            length=Length(min=3, max=3, avg=3.0, p95=3.0),
        )

    # Three values, enumerated in full, so this one's counts have to add up.
    status = ColumnStats(
        sql_type="text",
        nullable=False,
        null_count=0,
        null_rate=0.0,
        cardinality=3,
        cardinality_ratio=0.015,
        cardinality_method="exact",
        values=(
            ValueCount(value="a", count=100),
            ValueCount(value="b", count=60),
            ValueCount(value="c", count=40),
        ),
        values_coverage=1.0,
        distribution="imbalanced",
        length=Length(min=1, max=1, avg=1.0, p95=1.0),
    )

    return {
        "public.curator_note": mock_table(
            "public.curator_note",
            columns(
                ("field_notes", "text"),
                ("institution", "text"),
                ("status", "text"),
                ("phone", "text"),
            ),
            {
                "field_notes": text_stats(),
                "institution": text_stats(),
                "status": status,
                "phone": text_stats(),
            },
            ddl="CREATE TABLE public.curator_note "
            "(field_notes text, institution text, status text, phone text);\n",
            samples={
                "field_notes": PROSE,
                "institution": EMAILS,
                "status": PROSE,
                "phone": PROSE,
            },
            row_count=200,
        ),
    }
