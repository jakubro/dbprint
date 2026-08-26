"""Format version checks per SPEC 5."""

from __future__ import annotations

from typing import Any

from .issue import Issue


def check(data: Any, path: str) -> list[Issue]:
    """Validate the format_version field in any artifact YAML."""

    if not isinstance(data, dict):
        return []

    if "format_version" not in data:
        return [
            Issue(
                path,
                "version.missing-format-version",
                "error",
                "Artifact missing required format_version field.",
                "§5.1",
            ),
        ]

    fv = data["format_version"]

    if not isinstance(fv, int) or fv < 1:
        return [
            Issue(
                path,
                "version.invalid-format-version",
                "error",
                f"format_version must be a positive integer; got {fv!r}.",
                "§5.1",
            ),
        ]
    elif fv != 1:
        return [
            Issue(
                path,
                "version.unknown-format-version",
                "error",
                f"format_version {fv} is not v1; this validator only handles MAJOR=1.",
                "§5.2",
            ),
        ]
    else:
        return []
