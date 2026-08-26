"""The adapter/engine convergence contract: mismatched type spellings produce non-conformant
prints, and an unnamed type still classifies by what the adapter measured (SPEC 3.1).
"""

from __future__ import annotations

from dbprint.spec.classification import base_type, classify


_THRESHOLD = 50


def test_datetime_mid_cardinality_is_temporal() -> None:
    result = classify(
        "datetime",
        cardinality=60,
        has_declared_fk=False,
        enumeration_threshold=_THRESHOLD,
    )
    assert result == "temporal"


def test_year_mid_cardinality_is_temporal() -> None:
    result = classify(
        "year",
        cardinality=60,
        has_declared_fk=False,
        enumeration_threshold=_THRESHOLD,
    )
    assert result == "temporal"


def test_datetime_full_cardinality_is_temporal() -> None:
    """Uniqueness is not a classification (SPEC 4.2) - a unique datetime stays temporal."""

    result = classify(
        "datetime",
        cardinality=1000,
        has_declared_fk=False,
        enumeration_threshold=_THRESHOLD,
    )
    assert result == "temporal"


def test_datetime_low_cardinality_is_categorical() -> None:
    result = classify(
        "datetime",
        cardinality=5,
        has_declared_fk=False,
        enumeration_threshold=_THRESHOLD,
    )
    assert result == "categorical"


def test_postgres_timestamp_unaffected() -> None:
    result = classify(
        "timestamp without time zone",
        cardinality=60,
        has_declared_fk=False,
        enumeration_threshold=_THRESHOLD,
    )
    assert result == "temporal"


def test_mysql_unsigned_integer_is_numeric() -> None:
    """MySQL 8.0.19+ reports `column_type` with no display width: `bigint unsigned`."""

    result = classify(
        "bigint unsigned",
        cardinality=1000,
        has_declared_fk=False,
        enumeration_threshold=_THRESHOLD,
    )
    assert result == "numeric"


def test_mysql_unsigned_zerofill_integer_is_numeric() -> None:
    result = classify(
        "int unsigned zerofill",
        cardinality=1000,
        has_declared_fk=False,
        enumeration_threshold=_THRESHOLD,
    )
    assert result == "numeric"


def test_a_measured_column_of_an_unnamed_type_is_text() -> None:
    """A Postgres `inet` column: no rule names it, but the adapter measured it."""

    result = classify(
        "inet",
        cardinality=1000,
        has_declared_fk=False,
        enumeration_threshold=_THRESHOLD,
    )
    assert result == "text"


def test_an_unmeasured_column_of_an_unnamed_type_is_unsupported() -> None:
    """The same type name, with no cardinality: the adapter declined to profile it."""

    result = classify(
        "inet",
        cardinality=None,
        has_declared_fk=False,
        enumeration_threshold=_THRESHOLD,
    )
    assert result == "unsupported"


def test_a_genuinely_unsupported_type_stays_unsupported_even_if_measured() -> None:
    """`bytea` is on the format's own list; a stray cardinality does not rescue it."""

    result = classify(
        "bytea",
        cardinality=1000,
        has_declared_fk=False,
        enumeration_threshold=_THRESHOLD,
    )
    assert result == "unsupported"


def test_an_unqueried_column_of_an_unnamed_type_is_text() -> None:
    """SPEC 3.3: under `catalog_only`, an ordinary type never falls to `unsupported`."""

    result = classify(
        "inet",
        cardinality=None,
        has_declared_fk=False,
        enumeration_threshold=_THRESHOLD,
        catalog_only=True,
    )
    assert result == "text"


def test_an_unqueried_binary_column_still_reaches_unsupported() -> None:
    """The genuinely unsupported set - binary, array, composite - is matched first either way."""

    result = classify(
        "bytea",
        cardinality=None,
        has_declared_fk=False,
        enumeration_threshold=_THRESHOLD,
        catalog_only=True,
    )
    assert result == "unsupported"


def test_an_unqueried_declared_fk_column_is_foreign_key_candidate() -> None:
    """A declared or naming-inferred FK is a catalog-derived fact, unaffected by the marker."""

    result = classify(
        "bigint",
        cardinality=None,
        has_declared_fk=True,
        enumeration_threshold=_THRESHOLD,
        catalog_only=True,
    )
    assert result == "foreign_key_candidate"


def test_an_unqueried_column_never_classifies_categorical() -> None:
    """`categorical` needs a `cardinality` the marker forbids - unreachable, not just unlikely."""

    result = classify(
        "integer",
        cardinality=None,
        has_declared_fk=False,
        enumeration_threshold=_THRESHOLD,
        catalog_only=True,
    )
    assert result != "categorical"


def test_base_type_strips_mysql_unsigned() -> None:
    assert base_type("bigint unsigned") == "bigint"


def test_base_type_strips_mysql_unsigned_zerofill() -> None:
    assert base_type("int(10) unsigned zerofill") == "int"


def test_base_type_strips_precision_wherever_it_falls() -> None:
    """`timestamp(3) with time zone`: the qualifier follows the precision group."""

    assert base_type("timestamp(3) with time zone") == "timestamp with time zone"


def test_base_type_strips_trailing_precision() -> None:
    assert base_type("numeric(10,2)") == "numeric"


def test_base_type_does_not_confuse_precision_for_a_qualifier() -> None:
    """`double precision` carries no MySQL qualifier substring collision."""

    assert base_type("double precision") == "double precision"
