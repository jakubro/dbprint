"""The adapter/engine convergence contract: mismatched type spellings produce non-conformant
prints, and an unnamed type still classifies by what the adapter measured (SPEC 3.1).
"""

from __future__ import annotations

import importlib
from types import ModuleType

from dbprint.cli.adapter_registry import ADAPTERS
from dbprint.spec.classification import (
    _BOOLEAN_TYPES,
    _JSON_TYPES,
    _NUMERIC_TYPES,
    _TEMPORAL_TYPES,
    base_type,
    classify,
    has_day_resolution,
    is_nullable_type,
)


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


def test_duckdbs_unsigned_and_hugeint_family_is_numeric() -> None:
    for sql_type in ("hugeint", "ubigint", "uinteger", "usmallint", "utinyint"):
        result = classify(sql_type, 1000, False, _THRESHOLD)
        assert result == "numeric", (sql_type, result)


def test_bigquerys_bignumeric_is_numeric() -> None:
    result = classify("BIGNUMERIC", 1000, False, _THRESHOLD)
    assert result == "numeric"


def test_redshifts_super_is_json() -> None:
    result = classify("super", 1000, False, _THRESHOLD)
    assert result == "json"


def test_mysqls_dec_and_fixed_synonyms_are_numeric() -> None:
    """Both are pure DDL synonyms for DECIMAL; MariaDB's information_schema normalizes them
    away before an adapter ever sees the string, but the adapter's own tuple names them.
    """

    for sql_type in ("dec", "fixed"):
        result = classify(sql_type, 1000, False, _THRESHOLD)
        assert result == "numeric", (sql_type, result)


def test_clickhouses_date32_has_no_day_to_truncate_to() -> None:
    """Date32 is always its own day-truncation, same as Date - `quantized_count` is neither
    computed nor required (SPEC 2.2.3's day-resolution footnote).
    """

    assert has_day_resolution("Date32") is False


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


def test_is_nullable_type_matches_a_bare_wrapper() -> None:
    assert is_nullable_type("Nullable(Int32)") is True


def test_is_nullable_type_matches_nullable_nested_under_another_wrapper() -> None:
    """ClickHouse's canonical nullable-low-cardinality spelling nests `Nullable` inside
    `LowCardinality`, which an anchored `^Nullable\\(...\\)$` test never reaches.
    """

    assert is_nullable_type("LowCardinality(Nullable(String))") is True


def test_is_nullable_type_is_false_for_a_non_nullable_wrapped_type() -> None:
    assert is_nullable_type("LowCardinality(String)") is False


def test_is_nullable_type_is_false_for_an_unwrapped_type() -> None:
    assert is_nullable_type("String") is False


def test_base_type_does_not_confuse_precision_for_a_qualifier() -> None:
    """`double precision` carries no MySQL qualifier substring collision."""

    assert base_type("double precision") == "double precision"


_SHARED_TABLES: dict[str, tuple[str, ...]] = {
    "_BOOLEAN_TYPES": _BOOLEAN_TYPES,
    "_JSON_TYPES": _JSON_TYPES,
    "_TEMPORAL_TYPES": _TEMPORAL_TYPES,
    "_NUMERIC_TYPES": _NUMERIC_TYPES,
}


def _adapter_stats_module(adapter_cls: type) -> ModuleType:
    """Import the vendor package's `stats` module beside its registered `adapter` module."""

    package = adapter_cls.__module__.rsplit(".", 1)[0]

    return importlib.import_module(f"{package}.stats")


def test_every_registered_adapters_own_type_spellings_are_known_to_the_shared_tables() -> None:
    """The convergence contract, driven off the adapter registry rather than a hand-kept
    vendor list - a ninth adapter is swept the moment it declares its own tuples (SPEC 3.1).
    """

    unknown = [
        (vendor, table_name, sql_type)
        for vendor, adapter_cls in ADAPTERS.items()
        for table_name, shared in _SHARED_TABLES.items()
        for sql_type in getattr(_adapter_stats_module(adapter_cls), table_name, ())
        if base_type(sql_type) not in shared
    ]

    assert unknown == []
