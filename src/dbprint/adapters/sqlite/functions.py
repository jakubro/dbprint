"""The functions SQLite lacks and the statements call, registered on each connection: the digest
the seeded draw orders by, the sketch's 64-bit hash, the row draw, and the canonical instant.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from typing import Any


_FRACTION_RE = re.compile(r"(?<=\d\d:\d\d:\d\d)\.(\d+)")


def register(connection: Any) -> None:
    """Register every function on `connection`, deterministic so SQLite may fold repeated calls."""

    connection.create_function("DBPRINT_MD5", 1, md5_hex, deterministic=True)
    connection.create_function("DBPRINT_LOW64", 1, low64, deterministic=True)
    connection.create_function("DBPRINT_DRAW", 2, draw, deterministic=True)
    connection.create_function("DBPRINT_ISO", 1, iso_instant, deterministic=True)


def md5_hex(value: Any) -> str | None:
    """Lowercase hex MD5 of a text's UTF-8 bytes or a blob's own bytes; NULL on NULL."""

    return None if value is None else hashlib.md5(_bytes(value), usedforsecurity=False).hexdigest()


def low64(value: Any) -> int | None:
    """Low 64 bits of MD5(`value`), signed - SQLite has no unsigned integer (SPEC 2.2.14)."""

    if value is None:
        return None

    digest = hashlib.md5(_bytes(value), usedforsecurity=False).digest()

    return int.from_bytes(digest[8:], "big", signed=True)


def draw(seed: Any, key: Any) -> float:
    """A uniform draw in [0, 1) that depends on the table's seed and the row's key alone."""

    digest = hashlib.md5(_bytes(seed) + b"\x00" + _bytes(key), usedforsecurity=False).digest()

    return int.from_bytes(digest[:8], "big") / 2**64


def iso_instant(value: Any) -> str | None:
    """A stored date or date-time text in SPEC 2.2.14's canonical form; NULL where it reads none.

    A value carrying an offset is an instant, normalized to UTC and marked `Z`.
    """

    if not isinstance(value, str):
        return None

    text = value.strip()
    fraction = _FRACTION_RE.search(text)
    digits = (fraction.group(1) + "0" * 9)[:9] if fraction else "0" * 9
    whole = _FRACTION_RE.sub("", text)

    try:
        if len(whole) == len("YYYY-MM-DD"):
            return datetime.fromisoformat(whole).date().isoformat()

        parsed = datetime.fromisoformat(whole)
    except ValueError:
        return None

    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(UTC).replace(tzinfo=None)

    stamp = parsed.strftime("%Y-%m-%dT%H:%M:%S")

    if digits[6:] != "000":
        stamp += f".{digits}"
    elif digits[:6] != "000000":
        stamp += f".{digits[:6]}"

    return stamp + ("Z" if "Z" in whole or _offset(whole) else "")


def _offset(text: str) -> bool:
    return re.search(r"[+-]\d\d:?\d\d$", text[len("YYYY-MM-DD") :]) is not None


def _bytes(value: Any) -> bytes:
    if isinstance(value, bytes):
        return value

    return str(value).encode("utf-8")
