"""YAML dumping with representers for the driver scalars `SafeDumper` rejects.

A rejection fails the whole table, so each type is normalized without losing precision:
floats stay positional (SPEC 2.2.6), and a string is quoted whenever a YAML v1.2 parser
would read it back as null/bool/int/float. An unrecognized type raises rather than
degrading to `str()`, so a malformed value cannot reach an artifact.
"""

from __future__ import annotations

import datetime
import decimal
import math
import re
import unicodedata
import uuid
from typing import Any

import yaml

from dbprint.spec.rounding import UnrepresentableValue
from dbprint.spec.value_text import scalar_text


class ArtifactDumper(yaml.SafeDumper):
    """SafeDumper subclass carrying the artifact representer set."""


def _represent_uuid(dumper: yaml.SafeDumper, data: Any) -> yaml.ScalarNode:
    return dumper.represent_scalar("tag:yaml.org,2002:str", scalar_text(data))


def _represent_decimal(dumper: yaml.SafeDumper, data: Any) -> yaml.ScalarNode:
    if not data.is_finite():
        return _represent_float(dumper, float(data))

    return dumper.represent_scalar("tag:yaml.org,2002:float", scalar_text(data))


def _represent_datetime(dumper: yaml.SafeDumper, data: Any) -> yaml.ScalarNode:
    return dumper.represent_scalar("tag:yaml.org,2002:str", scalar_text(data))


def _represent_float(dumper: yaml.SafeDumper, data: Any) -> yaml.ScalarNode:
    """Emit a float positionally per SPEC 2.2.6, losslessly - PyYAML's `repr` fallback switches
    to the exponent form SPEC 2.2.6 disallows below 1e-4 and at/above 1e16.
    """

    if not math.isfinite(data):
        return yaml.SafeDumper.represent_float(dumper, data)

    return dumper.represent_scalar("tag:yaml.org,2002:float", scalar_text(data))


# YAML v1.2 core schema productions (yaml.org spec 10.3.2): what a conformant 1.2 parser
# resolves away from string. Sign is grammatical only on decimal int, .inf and the general
# float form, so a signed 0x/0o int or .nan stays a string.
_YAML12_NULL_RE = re.compile(r"~|null|Null|NULL")
_YAML12_BOOL_RE = re.compile(r"true|True|TRUE|false|False|FALSE")
_YAML12_INT_RE = re.compile(r"[-+]?[0-9]+|0o[0-7]+|0x[0-9a-fA-F]+")
_YAML12_FLOAT_RE = re.compile(
    r"[-+]?\.(inf|Inf|INF)|\.(nan|NaN|NAN)|[-+]?(\.[0-9]+|[0-9]+(\.[0-9]*)?)([eE][-+]?[0-9]+)?",
)


def _looks_like_yaml12_scalar(text: str) -> bool:
    """True when a YAML v1.2 core-schema resolver would retype `text` away from string."""

    if not text:
        return False

    return bool(
        _YAML12_NULL_RE.fullmatch(text)
        or _YAML12_BOOL_RE.fullmatch(text)
        or _YAML12_INT_RE.fullmatch(text)
        or _YAML12_FLOAT_RE.fullmatch(text),
    )


def _represent_str(dumper: yaml.SafeDumper, data: str) -> yaml.ScalarNode:
    """Force single-quoting where a YAML v1.2 parser would retype the scalar.

    Leaving `style` unset preserves PyYAML's own quoting of what YAML v1.1 would retype;
    this only adds the shapes 1.1 misses, chiefly the exponent-form float production.
    """

    if "\x85" in data:
        # PyYAML writes NEL raw in a plain or single-quoted scalar, and its reader folds it as a
        # line break; only the double-quoted style escapes it.
        style = '"'
    elif _looks_like_yaml12_scalar(data):
        style = "'"
    else:
        style = None

    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


def _represent_timedelta(dumper: yaml.SafeDumper, data: Any) -> yaml.ScalarNode:
    return dumper.represent_scalar("tag:yaml.org,2002:str", scalar_text(data))


def _refuse(dumper: yaml.SafeDumper, data: Any) -> yaml.Node:
    """Raise where `SafeDumper` would write `!!set`/`!!binary` or fail with an anonymous error."""

    raise UnrepresentableValue(data)


def _register_representers() -> None:
    ArtifactDumper.add_representer(uuid.UUID, _represent_uuid)
    ArtifactDumper.add_representer(decimal.Decimal, _represent_decimal)
    ArtifactDumper.add_representer(float, _represent_float)
    ArtifactDumper.add_representer(str, _represent_str)
    ArtifactDumper.add_representer(datetime.datetime, _represent_datetime)
    ArtifactDumper.add_representer(datetime.date, _represent_datetime)
    ArtifactDumper.add_representer(datetime.time, _represent_datetime)
    ArtifactDumper.add_representer(datetime.timedelta, _represent_timedelta)

    for refused in (bytes, bytearray, memoryview, set, frozenset, None):
        ArtifactDumper.add_representer(refused, _refuse)


_register_representers()


def dump_yaml(payload: Any) -> str:
    """Dump `payload` to YAML using the dbprint artifact representer set."""

    return yaml.dump(
        payload,
        Dumper=ArtifactDumper,
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
    )


def spell_inline(value: Any) -> str:
    """Spell one value on one physical line such that `yaml.safe_load` of the result equals it.

    A string with whitespace is quoted, and one with a line break or invisible character escaped.
    """

    if isinstance(value, list | tuple):
        return "[" + ", ".join(_spell_nested(item) for item in value) + "]"

    if isinstance(value, dict):
        pairs = (f"{_spell_nested(k)}: {_spell_nested(v)}" for k, v in value.items())

        return "{" + ", ".join(pairs) + "}"

    if isinstance(value, str) and any(_escaped(char) for char in value):
        return _double_quoted(value)

    text = _dump_flow(value)

    if (
        isinstance(value, str)
        and not text.startswith(("'", '"'))
        and any(c.isspace() for c in text)
    ):
        return "'" + value.replace("'", "''") + "'"

    return text


def spell_value(value: Any) -> str:
    """Spell one stored value for a human or agent: `NULL` for a genuine null, else `spell_inline`."""

    return "NULL" if value is None else spell_inline(value)


def spell_literal(value: Any) -> str:
    """Spell one value as an SQL literal: strings and timestamps single-quoted with `''` doubled.

    Numbers, booleans and `NULL` stay bare; a string holding an invisible character is escaped.
    """

    if value is None:
        return "NULL"

    if isinstance(value, bool):
        return "true" if value else "false"

    if isinstance(value, int | float | decimal.Decimal):
        return scalar_text(value)

    if isinstance(value, str):
        if any(_escaped(char) for char in value):
            return _double_quoted(value)

        return "'" + value.replace("'", "''") + "'"

    return "'" + scalar_text(value).replace("'", "''") + "'"


# Printed raw these read as nothing or as an ordinary space; U+0020 is the one space left bare.
_ESCAPED_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp", "Co", "Cs", "Cn"})
_SHORT_ESCAPES = {"\\": "\\\\", '"': '\\"', "\n": "\\n", "\r": "\\r", "\t": "\\t", "\0": "\\0"}


def _escaped(char: str) -> bool:
    category = unicodedata.category(char)

    return category in _ESCAPED_CATEGORIES or (category == "Zs" and char != " ")


def _escape(char: str) -> str:
    if char in _SHORT_ESCAPES:
        return _SHORT_ESCAPES[char]

    if not _escaped(char):
        return char

    code = ord(char)

    if code <= 0xFF:
        return f"\\x{code:02x}"

    return f"\\u{code:04x}" if code <= 0xFFFF else f"\\U{code:08x}"


def _spell_nested(value: Any) -> str:
    if isinstance(value, list | tuple | dict):
        return spell_inline(value)

    if isinstance(value, str) and any(_escaped(char) for char in value):
        return _double_quoted(value)

    # Dumped as a one-element sequence so PyYAML quotes it for a flow context, not a document.
    return _dump_flow([value])[1:-1]


def _dump_flow(value: Any) -> str:
    text = yaml.dump(
        value,
        Dumper=ArtifactDumper,
        default_flow_style=True,
        sort_keys=False,
        allow_unicode=True,
        width=math.inf,
    )

    return text.removesuffix("\n...\n").removesuffix("\n")


def _double_quoted(value: str) -> str:
    return '"' + "".join(_escape(char) for char in value) + '"'
