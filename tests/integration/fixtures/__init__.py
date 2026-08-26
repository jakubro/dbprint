"""Integration-test fixtures: schema + data + selective sanity-check expectations."""

from __future__ import annotations

from pathlib import Path


FIXTURES_DIR = Path(__file__).parent
SCHEMA_SQL = (FIXTURES_DIR / "schema.sql").read_text()
DATA_SQL = (FIXTURES_DIR / "data.sql").read_text()
