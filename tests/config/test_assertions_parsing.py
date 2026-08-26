"""Config loader picks up the assertions block per ASSERTIONS.md 1.

The reference `.dbprint.yaml` shipped alongside the production example already carries a real
assertions block, commented out; these tests uncomment it, extending it with a `queries`
entry over the same real column where needed, rather than inventing a parallel fixture.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dbprint.config import load_project


_REFERENCE_CONFIG_PATH = (
    Path(__file__).resolve().parents[2] / "docs/format/v1/examples/production/.dbprint.yaml"
)
_REFERENCE_CONFIG = _REFERENCE_CONFIG_PATH.read_text()

# The exact commented block at the end of the reference config.
_COMMENTED_BLOCK = """\
#    # Data-quality checks evaluated by `dbprint check`, in the grammar at
#    # ../../../../ASSERTIONS.md.
#    assertions:
#      tables:
#        seedbank.collector:
#          columns:
#            email:
#              null_rate: 0
"""

_TABLES_ONLY_BLOCK = """\
    assertions:
      tables:
        seedbank.collector:
          columns:
            email:
              null_rate: 0
"""

_QUERIES_ONLY_BLOCK = """\
    assertions:
      queries:
        - name: no_duplicate_emails
          sql: SELECT count(*) - count(DISTINCT email) FROM seedbank.collector
          expect: 0
"""

_FULL_BLOCK = """\
    assertions:
      tables:
        seedbank.collector:
          columns:
            email:
              null_rate: 0
      queries:
        - name: no_duplicate_emails
          severity: warning
          sql: SELECT count(*) - count(DISTINCT email) FROM seedbank.collector
          expect: 0
"""


def _write_config(tmp_path: Path, replacement: str | None) -> None:
    """The shipped reference config, its commented assertions block swapped for `replacement`.

    Left commented out, and so absent, when `replacement` is None.
    """

    text = _REFERENCE_CONFIG

    if replacement is not None:
        assert _COMMENTED_BLOCK in text
        text = text.replace(_COMMENTED_BLOCK, replacement)

    (tmp_path / ".dbprint.yaml").write_text(text)


class TestAssertionsBlockParsing:
    def test_absent_block_yields_empty_raw(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The shipped reference config ships this block commented out."""

        _write_config(tmp_path, None)
        monkeypatch.chdir(tmp_path)
        cfg = load_project()
        assert cfg.connections["production"].assertions_raw == {}

    def test_tables_only_block_captured(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_config(tmp_path, _TABLES_ONLY_BLOCK)
        monkeypatch.chdir(tmp_path)
        cfg = load_project()
        block = cfg.connections["production"].assertions_raw
        assert "tables" in block
        assert "queries" not in block
        assert block["tables"]["seedbank.collector"]["columns"]["email"]["null_rate"] == 0

    def test_queries_block_captured(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write_config(tmp_path, _QUERIES_ONLY_BLOCK)
        monkeypatch.chdir(tmp_path)
        cfg = load_project()
        queries = cfg.connections["production"].assertions_raw["queries"]
        assert queries[0]["name"] == "no_duplicate_emails"
        assert queries[0]["expect"] == 0

    def test_full_block_captured(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write_config(tmp_path, _FULL_BLOCK)
        monkeypatch.chdir(tmp_path)
        cfg = load_project()
        block = cfg.connections["production"].assertions_raw
        assert "tables" in block and "queries" in block
        assert block["queries"][0]["severity"] == "warning"
