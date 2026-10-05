"""Column classification per SPEC v1, section 3.

`classify()` picks the first match per the SPEC 3.2 priority order. Pure: no I/O, no state.
"""

from __future__ import annotations

import re
from typing import Literal


RecordKind = Literal["struct", "union"]

Classification = Literal[
    "boolean",
    "json",
    "composite",
    "spatial",
    "vector",
    "foreign_key_candidate",
    "categorical",
    "temporal",
    "numeric",
    "binary",
    "text",
    "unsupported",
]


CANDIDATE_KEY_THRESHOLD = 0.9999

# Six-decimal rounding can carry a nonzero ratio to 0.0 or 1.0; these bounds keep a genuine
# zero distinguishable from "too small to show" (SPEC 2.2.6).
_RATIO_FLOOR = 0.000001
_RATIO_CEILING = 0.999999


def _floored(rounded: float, numerator: int) -> float:
    """A nonzero numerator never rounds all the way down to 0.0."""

    return _RATIO_FLOOR if numerator > 0 and rounded == 0.0 else rounded


def compute_cardinality_ratio(cardinality: int, rows_scanned: int) -> float:
    """The published `cardinality_ratio` (SPEC 2.2.6), rounded to six places.

    Single definition shared by adapters and the engine, so the 0.9999 candidate-key
    threshold falls the same way on both. A nonzero cardinality never rounds down to 0.0.
    """

    if not rows_scanned:
        return 0.0

    return _floored(round(cardinality / rows_scanned, 6), cardinality)


def is_candidate_key(cardinality: int, ratio: float) -> bool:
    """Whether an already-rounded ratio clears the SPEC 4.2 candidate-key threshold.

    Independent of `classify()` - a column of any classification can clear it.
    """

    return cardinality > 0 and ratio >= CANDIDATE_KEY_THRESHOLD


CandidateKeyException = Literal["measured_duplicates", "estimated"]


def compute_candidate_key_exception(
    cardinality: int,
    cardinality_ratio: float,
    cardinality_method: str,
    rows_scanned: int,
    null_count: int,
) -> CandidateKeyException | None:
    """The SPEC 4.2 `candidate_key_exception` marker (only relevant once ratio clears 0.9999).

    None at ratio 1.0 regardless of method. Below it: `measured_duplicates` when an exact count
    is short of the non-null scanned set, `estimated` when an estimate's error may straddle it.
    """

    if cardinality_ratio >= 1.0:
        return None

    if cardinality_method == "exact":
        return "measured_duplicates" if cardinality < rows_scanned - null_count else None

    return "estimated"


def compute_null_rate(null_count: int, rows_scanned: int) -> float:
    """The published `null_rate` (SPEC 2.2.6), rounded to six places.

    Single definition shared by all three adapters. Neither bound is reachable by rounding: a
    nonzero `null_count` never rounds down to 0.0, and a nonzero non-null count never rounds
    up to 1.0, since `null_rate: 1.0` is a defined sentinel (SPEC 2.2.7, 3.3).
    """

    if not rows_scanned:
        return 0.0

    rounded = round(null_count / rows_scanned, 6)
    non_null = rows_scanned - null_count

    if non_null > 0 and rounded == 1.0:
        return _RATIO_CEILING

    return _floored(rounded, null_count)


# MySQL has no native BOOLEAN - `BOOLEAN`/`BOOL` is an alias for `TINYINT(1)`, and `base_type()`
# strips the width that distinguishes it, so this is checked against the raw `sql_type`.
_MYSQL_BOOLEAN_TYPE = "tinyint(1)"

_BOOLEAN_TYPES = ("boolean", "bool")
_JSON_TYPES = ("json", "jsonb", "variant", "object", "super")
_TEMPORAL_TYPES = (
    "date",
    "date32",
    "time",
    "timestamp",
    "timestamp with time zone",
    "timestamp without time zone",
    "time with time zone",
    "time without time zone",
    "timestamp_ntz",
    "timestamp_ltz",
    "timestamp_tz",
    "datetime",
    "datetime64",
    "year",
    "timestamp_ns",
    "timestamp_ms",
    "timestamp_s",
    "time_ns",
    "time64",
)
_NUMERIC_TYPES = (
    "smallint",
    "integer",
    "bigint",
    "decimal",
    "numeric",
    "real",
    "double precision",
    "double",
    "float",
    "money",
    "number",
    "int",
    "tinyint",
    "mediumint",
    "int8",
    "int16",
    "int32",
    "int64",
    "int128",
    "int256",
    "uint8",
    "uint16",
    "uint32",
    "uint64",
    "uint128",
    "uint256",
    "float32",
    "float64",
    "decimal32",
    "decimal64",
    "decimal128",
    "decimal256",
    "hugeint",
    "ubigint",
    "uinteger",
    "usmallint",
    "utinyint",
    "bignumeric",
    "dec",
    "fixed",
    "uhugeint",
    "bignum",
    "bfloat16",
    "decfloat",
)
_CHARACTER_TYPES = (
    "varchar",
    "text",
    "char",
    "character varying",
    "character",
    "string",
    "uuid",
    "fixedstring",
    "tinytext",
    "mediumtext",
    "longtext",
)
_BINARY_TYPES = (
    "bytea",
    "blob",
    "tinyblob",
    "mediumblob",
    "longblob",
    "binary",
    "varbinary",
    "varbyte",
    "binary varying",
    "bytes",
    "image",
)
# OGC simple-features types. Postgres resolves its native `point` and `polygon` to `geometric`
# before classification, so they never land here.
_SPATIAL_TYPES = (
    "geometry",
    "geography",
    "point",
    "linestring",
    "polygon",
    "multipoint",
    "multilinestring",
    "multipolygon",
    "geometrycollection",
    "ring",
)
# Native embedding types: pgvector's three, MySQL's and Snowflake's VECTOR, ClickHouse's QBit.
_VECTOR_TYPES = ("vector", "halfvec", "sparsevec", "qbit")
_UNSUPPORTED_TYPES = (
    "record",
    "struct",
    "array",
    "map",
    "tuple",
    "nested",
    "aggregatefunction",
    "simpleaggregatefunction",
    "union",
)

# One or more trailing `[]`/`[n]` suffixes, read after every bracketed group is gone.
_ARRAY_SUFFIX_RE = re.compile(r"(\[\d*\])+$")

_FLOATING_TYPES = (
    "real",
    "double precision",
    "double",
    "float",
    "float4",
    "float8",
    "float32",
    "float64",
    "bfloat16",
)

_GROUP_CLOSERS = {"(": ")", "<": ">"}

# MySQL reports these inside `column_type` (`bigint unsigned`, `int unsigned zerofill`),
# with no separating paren for a base-name split to key on.
_MYSQL_NUMERIC_QUALIFIER_RE = re.compile(r"\b(unsigned|zerofill|signed)\b")

# ClickHouse names a type by wrapping it (`Nullable(Int32)`, `LowCardinality(String)`,
# `SimpleAggregateFunction(sum, UInt64)`) rather than qualifying it - unwrapping recurses to it.
# The wrapper name is captured (not just discarded) so `is_nullable_type` can share this same
# definition rather than testing nullability with a second, unanchored pattern.
_CLICKHOUSE_WRAPPER_RE = re.compile(
    r"^(nullable|lowcardinality|simpleaggregatefunction)\((.+)\)$",
)


def base_type(sql_type: str) -> str:
    """Lowercase type name with wrappers, every `(...)`/`<...>` group at any depth and MySQL's
    qualifiers stripped - the one normalization every adapter and `classify` share.
    """

    lowered = sql_type.lower()

    while (unwrapped := _unwrapped(lowered)) is not None:
        lowered = unwrapped[1]

    stripped = _without_groups(lowered)
    stripped = _MYSQL_NUMERIC_QUALIFIER_RE.sub("", stripped)

    return " ".join(stripped.split())


def stored_type(sql_type: str) -> str:
    """The type a ClickHouse `SimpleAggregateFunction(f, T)` stores, `T` as spelled; else `sql_type`."""

    match = re.fullmatch(r"(?is)\s*SimpleAggregateFunction\((.*)\)\s*", sql_type)
    arguments = top_level_arguments(match.group(1)) if match else []

    return arguments[1].strip() if len(arguments) > 1 else sql_type


def is_nullable_type(sql_type: str) -> bool:
    """Whether `sql_type` carries a `Nullable(...)` wrapper at any nesting depth.

    ClickHouse's `LowCardinality(Nullable(String))` nests it under a second wrapper, which an
    anchored test never matches, so this shares `base_type`'s own unwrapping regex.
    """

    lowered = sql_type.lower()

    while (unwrapped := _unwrapped(lowered)) is not None:
        if unwrapped[0] == "nullable":
            return True

        lowered = unwrapped[1]

    return False


def classify(
    sql_type: str,
    cardinality: int | None,
    has_declared_fk: bool,
    enumeration_threshold: int,
    *,
    catalog_only: bool = False,
    has_parts: bool = False,
) -> Classification:
    """Return the v1 classification for a column per SPEC 3.2 priority order.

    `has_declared_fk` covers a declared or naming-inferred source, both catalog-derived, so it
    participates under `catalog_only` too. `cardinality=None` means either the adapter declined
    to profile (SPEC 3.1) or nothing was queried (`catalog_only`, SPEC 2.2.15); the two differ
    only in the unmatched-type fallthrough - `unsupported` and `text` respectively (SPEC 3.3).
    `has_parts` says the producer descended into the column, which makes it `composite` unless
    its type is JSON (SPEC 3.1).
    """

    if has_parts and not is_json_type(sql_type):
        return "composite"
    elif _matches(base_type(sql_type), _UNSUPPORTED_TYPES) or is_array_type(sql_type):
        return "unsupported"
    elif is_boolean_type(sql_type):
        return "boolean"
    elif is_json_type(sql_type):
        return "json"
    elif is_spatial_type(sql_type):
        return "spatial"
    elif is_vector_type(sql_type):
        return "vector"
    elif has_declared_fk:
        return "foreign_key_candidate"
    elif cardinality is not None and cardinality <= enumeration_threshold:
        return "categorical"
    elif is_temporal_type(sql_type):
        return "temporal"
    elif is_numeric_type(sql_type):
        return "numeric"
    elif is_binary_type(sql_type):
        return "binary"
    elif _matches(base_type(sql_type), _CHARACTER_TYPES) or cardinality is not None or catalog_only:
        return "text"
    else:
        return "unsupported"


def is_string_like_type(sql_type: str) -> bool:
    """Whether `sql_type` could hold a string value, by elimination against the other classes -
    the shared test the matrix and every adapter's Phase A use to decide whether `length` applies.
    """

    return not (
        _matches(base_type(sql_type), _UNSUPPORTED_TYPES)
        or is_array_type(sql_type)
        or is_boolean_type(sql_type)
        or is_json_type(sql_type)
        or is_temporal_type(sql_type)
        or is_numeric_type(sql_type)
        or is_binary_type(sql_type)
        or is_spatial_type(sql_type)
        or is_vector_type(sql_type)
    )


# The temporal shapes with no day to truncate to (SPEC 2.2.3): DATE and DATE32 are always
# their own day-truncation, TIME carries no date at all, YEAR carries neither.
_NO_DAY_TEMPORAL_TYPES = (
    "date",
    "date32",
    "time",
    "time with time zone",
    "time without time zone",
    "time_ns",
    "time64",
    "year",
)


def has_day_resolution(sql_type: str) -> bool:
    """Whether `sql_type` is a temporal type `quantized_count`'s day-truncation applies to - the
    shared test the matrix and every adapter's temporal fetch use to decide whether to compute.
    """

    base = base_type(sql_type)

    return _matches(base, _TEMPORAL_TYPES) and not _matches(base, _NO_DAY_TEMPORAL_TYPES)


# TIME (with/without time zone) carries no date at all; YEAR carries only a year number.
# Neither has a calendar day/week/month a bucketing truncation could place.
_NO_CALENDAR_TEMPORAL_TYPES = (
    "time",
    "time with time zone",
    "time without time zone",
    "time_ns",
    "time64",
    "year",
)


def has_calendar_component(sql_type: str) -> bool:
    """Whether `sql_type` carries a calendar date `timeline` bucketing can truncate to - the
    anchor rule (SPEC 2.2.16) uses it, so `probe_timeline` can assume a calendar type.
    """

    base = base_type(sql_type)

    return _matches(base, _TEMPORAL_TYPES) and not _matches(base, _NO_CALENDAR_TEMPORAL_TYPES)


def is_numeric_type(sql_type: str) -> bool:
    """Whether `sql_type` belongs to the numeric family `classify` reads as numbers."""

    return _matches(base_type(sql_type), _NUMERIC_TYPES)


def is_temporal_type(sql_type: str) -> bool:
    """Whether `sql_type` belongs to the temporal family - a date, instant, clock time or year."""

    return _matches(base_type(sql_type), _TEMPORAL_TYPES)


def is_spatial_type(sql_type: str) -> bool:
    """Whether `sql_type`'s values are OGC simple-features geometries (SPEC 3.1)."""

    return _matches(base_type(sql_type), _SPATIAL_TYPES)


def is_vector_type(sql_type: str) -> bool:
    """Whether `sql_type` is a native embedding type (SPEC 3.1), never a plain float array."""

    return _matches(base_type(sql_type), _VECTOR_TYPES)


def is_binary_type(sql_type: str) -> bool:
    """Whether `sql_type` holds bytes rather than characters - a binary string of any width."""

    return _matches(base_type(sql_type), _BINARY_TYPES)


def is_boolean_type(sql_type: str) -> bool:
    """Whether `sql_type` is a boolean, MySQL's `tinyint(1)` spelling included."""

    return _matches(base_type(sql_type), _BOOLEAN_TYPES) or _is_mysql_boolean_type(sql_type)


def is_json_type(sql_type: str) -> bool:
    """Whether `sql_type` is a semi-structured document type."""

    return _matches(base_type(sql_type), _JSON_TYPES)


def is_recognised_type(sql_type: str) -> bool:
    """Whether some shared table names `sql_type` - false means SPEC 3.3's fallthrough decides it."""

    base = base_type(sql_type)

    return (
        is_array_type(sql_type)
        or is_boolean_type(sql_type)
        or any(
            _matches(base, table)
            for table in (
                _UNSUPPORTED_TYPES,
                _BINARY_TYPES,
                _SPATIAL_TYPES,
                _VECTOR_TYPES,
                _JSON_TYPES,
                _TEMPORAL_TYPES,
                _NUMERIC_TYPES,
                _CHARACTER_TYPES,
            )
        )
    )


def element_type(sql_type: str) -> str | None:
    """The type of one element of the array `sql_type` names, or None where it names none.

    Strips one trailing `[]`/`[n]`, or unwraps one `ARRAY<T>`/`Array(T)`/`array<T>`; a bare
    `ARRAY` (Snowflake's semi-structured array) says nothing of its elements.
    """

    text = sql_type.strip()
    suffix = re.search(r"\[\d*\]$", text)

    if suffix is not None and is_array_type(text):
        return text[: suffix.start()].rstrip()

    match = re.fullmatch(r"(?is)array\s*([<(])(.*)([>)])", text)

    if match is None or _GROUP_CLOSERS[match.group(1)] != match.group(3):
        return None

    return match.group(2).strip()


def record_members(sql_type: str) -> tuple[RecordKind, list[tuple[str, str]]] | None:
    """The members the record or union type `sql_type` declares, in order, or None for another.

    Reads duckdb `STRUCT(a T)`/`UNION(a T)`, BigQuery `STRUCT<a T>`, Databricks `struct<a:T>` and
    ClickHouse `Tuple(a T)`/`Variant(T1, T2)`; an unnamed tuple element is named by its 1-based
    position and a variant member by its type. A quoted name is unquoted.
    """

    text = sql_type.strip()
    nullable = re.fullmatch(r"(?is)nullable\((.*)\)", text)
    text = nullable.group(1).strip() if nullable else text
    match = re.fullmatch(r"(?is)(struct|union|tuple|variant)\s*([<(])(.*)([>)])", text)

    if match is None or _GROUP_CLOSERS[match.group(2)] != match.group(4):
        return None

    word = match.group(1).lower()
    arguments = [a.strip() for a in top_level_arguments(match.group(3)) if a.strip()]

    if word == "variant":
        return "union", [(argument, argument) for argument in arguments]

    named = [_named_member(argument, colon=match.group(2) == "<") for argument in arguments]

    if word == "tuple" and not all(named):
        return "struct", [(str(i), a) for i, a in enumerate(arguments, start=1)]

    if not all(named):
        return None

    return ("union" if word == "union" else "struct"), [m for m in named if m is not None]


def map_types(sql_type: str) -> tuple[str, str] | None:
    """The key and value types the map type `sql_type` declares, or None for another type.

    Reads duckdb and Snowflake `MAP(K, V)`, Databricks `map<K,V>` and ClickHouse `Map(K, V)`.
    """

    match = re.fullmatch(r"(?is)map\s*([<(])(.*)([>)])", sql_type.strip())

    if match is None or _GROUP_CLOSERS[match.group(1)] != match.group(3):
        return None

    arguments = [a.strip() for a in top_level_arguments(match.group(2))]

    return (arguments[0], arguments[1]) if len(arguments) == 2 and all(arguments) else None


def is_floating_type(sql_type: str) -> bool:
    """Whether `sql_type` holds binary floating-point values, whose exact values pool no domain."""

    return _matches(base_type(sql_type), _FLOATING_TYPES)


def is_array_type(sql_type: str) -> bool:
    """Whether `sql_type` ends in `[]` or `[n]` suffixes at depth 0 - `INTEGER[2][2]`, not a
    bracket inside a struct field's own type.
    """

    return _ARRAY_SUFFIX_RE.search(_without_groups(sql_type).rstrip()) is not None


def _unwrapped(lowered: str) -> tuple[str, str] | None:
    """The wrapper name and the type it holds, or None when `lowered` wraps nothing.

    `SimpleAggregateFunction(f, T, ...)` stores plain values of `T`, its first type argument;
    one with no type argument is left wrapped, so it still declines.
    """

    match = _CLICKHOUSE_WRAPPER_RE.match(lowered)

    if match is None:
        return None

    wrapper, inner = match.groups()

    if wrapper != "simpleaggregatefunction":
        return wrapper, inner

    arguments = top_level_arguments(inner)

    return (wrapper, arguments[1].strip()) if len(arguments) > 1 else None


def top_level_arguments(text: str) -> list[str]:
    """`text` split at the commas outside every group and quote, each piece as written."""

    parts: list[str] = [""]
    closers: list[str] = []
    quote: str | None = None

    for char in text:
        if quote is not None:
            quote = None if char == quote else quote
        elif char in "\"'`":
            quote = char
        elif char in _GROUP_CLOSERS:
            closers.append(_GROUP_CLOSERS[char])
        elif closers and char == closers[-1]:
            closers.pop()
        elif char == "," and not closers:
            parts.append("")
            continue

        parts[-1] += char

    return parts


def _matches(base: str, types: tuple[str, ...]) -> bool:
    return base in types


def _named_member(argument: str, *, colon: bool) -> tuple[str, str] | None:
    if argument[:1] in '"`':
        mark = argument[0]
        quoted = re.match(rf"{mark}((?:[^{mark}]|{mark}{{2}})*){mark}", argument)

        if quoted is None:
            return None

        name = quoted.group(1).replace(mark * 2, mark)
        rest = argument[quoted.end() :]
    else:
        split = re.match(r"([A-Za-z_][A-Za-z0-9_$]*)(?=\s|:)", argument)

        if split is None:
            return None

        name, rest = split.group(1), argument[split.end() :]

    rest = rest.strip()

    if colon and rest.startswith(":"):
        rest = rest[1:].strip()

    return (name, rest) if rest else None


def _without_groups(sql_type: str) -> str:
    """`sql_type` with every balanced `(...)`/`<...>` group removed, quoted text skipped over.

    Qualifiers after a group survive (`timestamp(3) with time zone`).
    """

    out: list[str] = []
    closers: list[str] = []
    quote: str | None = None

    for char in sql_type:
        if quote is not None:
            quote = None if char == quote else quote
        elif char in "\"'`":
            quote = char
        elif char in _GROUP_CLOSERS:
            closers.append(_GROUP_CLOSERS[char])
            continue
        elif closers and char == closers[-1]:
            closers.pop()
            continue

        if not closers:
            out.append(char)

    return "".join(out)


def _is_mysql_boolean_type(sql_type: str) -> bool:
    return sql_type.strip().lower() == _MYSQL_BOOLEAN_TYPE
