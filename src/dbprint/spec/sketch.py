"""KMV key sketch per SPEC 2.2.14.

`low64_md5` and the pack/decode pair are one definition shared by the engine, conformance and
the adapter test suites, so none of the three can silently disagree on a sketch's values.
"""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Sequence
from typing import Literal

from .classification import base_type


SketchKind = Literal["integer", "decimal", "text", "boolean", "temporal"]

METHOD = "kmv_md5_lo64"
K = 1024

_INTEGER_TYPES = ("smallint", "integer", "bigint", "int", "tinyint", "mediumint")
_DECIMAL_TYPES = ("decimal", "numeric", "number")
_TEXT_TYPES = ("varchar", "text", "char", "character varying", "character", "string", "uuid")
_BOOLEAN_TYPES = ("boolean",)
_TEMPORAL_TYPES = (
    "date",
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
    "year",
)

_KIND_BY_TYPE: dict[str, SketchKind] = {
    **dict.fromkeys(_INTEGER_TYPES, "integer"),
    **dict.fromkeys(_DECIMAL_TYPES, "decimal"),
    **dict.fromkeys(_TEXT_TYPES, "text"),
    **dict.fromkeys(_BOOLEAN_TYPES, "boolean"),
    **dict.fromkeys(_TEMPORAL_TYPES, "temporal"),
}


def sketch_kind(sql_type: str) -> SketchKind | None:
    """The canonical encoding `sql_type` uses, or None when SPEC 2.2.14 defines none.

    Shares `classification.base_type`'s dialect normalization, so MySQL's `bigint unsigned`
    and Postgres's plain `bigint` resolve to the same kind. Floating-point types and every
    type outside the five SPEC 2.2.14 rows return None and are never sketched.
    """

    return _KIND_BY_TYPE.get(base_type(sql_type))


def canonical_form(value: object, kind: SketchKind) -> str:
    """SPEC 2.2.14's canonical byte string for one Python value under `kind`.

    The Python-side mirror of each adapter's SQL canonical-cast expression, used only where no
    database is in reach; a real adapter hashes in-database (SPEC 2.2.14). A `temporal` `value`
    MUST carry a `tzinfo` iff the column's type is timezone-aware, which `kind` cannot say.
    """

    if kind == "boolean":
        return "true" if value else "false"

    if kind == "temporal":
        return _canonical_temporal(value)

    return str(value)


def _canonical_temporal(value: object) -> str:
    isoformat = getattr(value, "isoformat", None)
    iso = isoformat() if callable(isoformat) else str(value)

    if getattr(value, "tzinfo", None) is not None:
        return iso.replace("+00:00", "Z")

    return iso


def low64_md5(canonical: str) -> int:
    """The low 64 bits of `canonical`'s MD5 digest, read big-endian, unsigned.

    Unkeyed and deterministic - no salt, no seed. Every adapter's in-database hash expression
    MUST reproduce this exact value for the same canonical bytes (SPEC 2.2.14's test vectors).
    """

    digest = hashlib.md5(canonical.encode("utf-8")).digest()

    return int.from_bytes(digest[8:], "big")


def pack_sketch(hashes: list[int]) -> str:
    """Ascending 64-bit big-endian unsigned integers, packed and base64-encoded (SPEC 2.2.14)."""

    ordered = sorted(hashes)
    raw = b"".join(h.to_bytes(8, "big", signed=False) for h in ordered)

    return base64.b64encode(raw).decode("ascii")


def decode_sketch(encoded: str) -> list[int] | None:
    """The reverse of `pack_sketch`, or None when `encoded` is not base64 of the right shape."""

    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError):
        return None

    if len(raw) % 8 != 0:
        return None

    return [int.from_bytes(raw[i : i + 8], "big") for i in range(0, len(raw), 8)]


def contains_value(encoded: str, value: object, kind: SketchKind) -> bool | None:
    """SPEC 2.2.14: whether `value` is a member of the sketched column's distinct set.

    Exact only below the sketch's retained size (`K`); at or above it membership is not
    answerable and this returns None. Raises ValueError on a malformed payload.
    """

    hashes = decode_sketch(encoded)

    if hashes is None:
        raise ValueError(f"not a valid sketch payload: {encoded!r}")

    if len(hashes) >= K:
        return None

    return low64_md5(canonical_form(value, kind)) in set(hashes)


_HASH_SPACE = 2**64  # sketch values are unsigned 64-bit hashes


def _theta(child_sketch: Sequence[int], parent_sketch: Sequence[int]) -> int:
    """The tighter of the two sketches' own retained thresholds (SPEC 2.2.14/2.3.10).

    A sketch retaining fewer than `k` values is exact, so its horizon is the whole hash space
    rather than its own maximum.
    """

    theta_child = max(child_sketch) if len(child_sketch) >= K else _HASH_SPACE
    theta_parent = max(parent_sketch) if len(parent_sketch) >= K else _HASH_SPACE

    return min(theta_child, theta_parent)


def estimate_intersection(child_sketch: Sequence[int], parent_sketch: Sequence[int]) -> int:
    """Bottom-k intersection estimator (SPEC 2.3.10) - `|A n B|`, unscaled when exact."""

    theta = _theta(child_sketch, parent_sketch)
    common = sum(1 for h in set(child_sketch) & set(parent_sketch) if h < theta)

    if theta >= _HASH_SPACE:
        return common

    return round(common * _HASH_SPACE / theta)


def answerable_count(child_sketch: Sequence[int], parent_sketch: Sequence[int]) -> int:
    """SPEC 2.2.14's `answerable count`, paired with `estimate_intersection`.

    The child's own retained hashes below the shared `theta` - the denominator the
    `1/sqrt(answerable count)` margin is sized against, not the match count.
    """

    theta = _theta(child_sketch, parent_sketch)

    return sum(1 for h in child_sketch if h < theta)


def answerable_subset_containment(
    child_sketch: Sequence[int],
    parent_sketch: Sequence[int],
) -> tuple[float, int] | None:
    """SPEC 2.2.14: an exhaustive child's match rate over its own answerable subset.

    Caller guarantees `child_sketch` is exhaustive and states whether the rate is exact or
    carries SPEC 2.2.14's margin (SPEC 2.3.10); `parent_sketch` may be either. Keys off the
    parent's own threshold alone, never `_theta`. Returns `(match rate, answerable count)`, or
    None when no child hash falls below the threshold - zero evidence, not zero overlap.
    """

    theta_parent = max(parent_sketch) if len(parent_sketch) >= K else _HASH_SPACE
    parent_set = set(parent_sketch)
    answerable = [h for h in child_sketch if h < theta_parent]

    if not answerable:
        return None

    matched = sum(1 for h in answerable if h in parent_set)

    return matched / len(answerable), len(answerable)
