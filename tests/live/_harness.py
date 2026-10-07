"""The SQL fixture-script steps every live e2e module shares.

A live module keeps its credentials, fixture driver and assertions about what the service printed.
"""

from __future__ import annotations

from pathlib import Path


def fixture_statements(fixture_dir: Path, engine: str) -> list[str]:
    """`schema.<engine>.sql` then `data.<engine>.sql`, one statement each, comment lines dropped."""

    return [
        statement
        for name in (f"schema.{engine}.sql", f"data.{engine}.sql")
        for statement in split_statements((fixture_dir / name).read_text())
    ]


def split_statements(text: str) -> list[str]:
    """One SQL script's statements, split on `;` with comment lines dropped."""

    lines = [ln for ln in text.splitlines() if not ln.strip().startswith("--")]

    return [stmt.strip() for stmt in "\n".join(lines).split(";") if stmt.strip()]
