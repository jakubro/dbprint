"""DDL checks per SPEC 2.1."""

from __future__ import annotations

from pathlib import Path

from .issue import Issue


def check(ddl_path: Path, path: str) -> list[Issue]:
    """Check one ddl.sql for encoding and content problems."""

    content = ddl_path.read_bytes()
    issues: list[Issue] = []

    if not content.strip():
        issues.append(
            Issue(
                path,
                "ddl.empty-file",
                "error",
                "ddl.sql exists but is empty.",
                "§2.1.4",
            ),
        )

        return issues

    if not content.endswith(b"\n"):
        issues.append(
            Issue(
                path,
                "ddl.missing-trailing-newline",
                "warning",
                "ddl.sql does not end with a newline (POSIX convention).",
                "§2.1.3",
            ),
        )

    return issues
