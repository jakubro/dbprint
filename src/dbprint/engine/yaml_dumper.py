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
import uuid
from typing import Any

import yaml


class ArtifactDumper(yaml.SafeDumper):
    """SafeDumper subclass carrying the artifact representer set."""


def _represent_uuid(dumper: yaml.SafeDumper, data: Any) -> yaml.ScalarNode:
    return dumper.represent_scalar("tag:yaml.org,2002:str", str(data))


def _represent_decimal(dumper: yaml.SafeDumper, data: Any) -> yaml.ScalarNode:
    return dumper.represent_scalar("tag:yaml.org,2002:str", str(data))


def _represent_datetime(dumper: yaml.SafeDumper, data: Any) -> yaml.ScalarNode:
    iso = data.isoformat()

    if iso.endswith("+00:00"):
        iso = iso[:-6] + "Z"

    return dumper.represent_scalar("tag:yaml.org,2002:str", iso)


def _represent_float(dumper: yaml.SafeDumper, data: Any) -> yaml.ScalarNode:
    """Emit a float positionally per SPEC 2.2.6, losslessly.

    PyYAML's `repr` fallback switches to the exponent form SPEC 2.2.6 disallows below 1e-4
    and at/above 1e16; `Decimal(repr(v))` then `f` expands it without re-rounding.
    """

    if not math.isfinite(data):
        return yaml.SafeDumper.represent_float(dumper, data)

    text = f"{decimal.Decimal(repr(data)):f}"

    # `f` drops an integral value's fractional part, which YAML would load back as an int.
    if "." not in text:
        text = f"{text}.0"

    return dumper.represent_scalar("tag:yaml.org,2002:float", text)


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

    style = "'" if _looks_like_yaml12_scalar(data) else None

    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


def _represent_timedelta(dumper: yaml.SafeDumper, data: Any) -> yaml.ScalarNode:
    # Integer arithmetic: total_seconds() drops microseconds near MySQL TIME's +/-838:59:59.
    microseconds = (data.days * 86400 + data.seconds) * 1_000_000 + data.microseconds
    sign = "-" if microseconds < 0 else ""
    seconds, fraction = divmod(abs(microseconds), 1_000_000)
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)

    clock = f"{sign}{hours:02d}:{minutes:02d}:{seconds:02d}"

    if fraction:
        clock = f"{clock}.{fraction:06d}"

    return dumper.represent_scalar("tag:yaml.org,2002:str", clock)


def _register_representers() -> None:
    ArtifactDumper.add_representer(uuid.UUID, _represent_uuid)
    ArtifactDumper.add_representer(decimal.Decimal, _represent_decimal)
    ArtifactDumper.add_representer(float, _represent_float)
    ArtifactDumper.add_representer(str, _represent_str)
    ArtifactDumper.add_representer(datetime.datetime, _represent_datetime)
    ArtifactDumper.add_representer(datetime.date, _represent_datetime)
    ArtifactDumper.add_representer(datetime.time, _represent_datetime)
    ArtifactDumper.add_representer(datetime.timedelta, _represent_timedelta)


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
