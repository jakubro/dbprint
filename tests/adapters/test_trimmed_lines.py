"""Every adapter's DDL leaves no trailing whitespace on a line and ends in one newline (SPEC 2.1.3)."""

from __future__ import annotations

import pytest

from dbprint.adapters.bigquery import ddl as bigquery_ddl
from dbprint.adapters.redshift import ddl as redshift_ddl


@pytest.mark.parametrize("normalize", [redshift_ddl.normalize, bigquery_ddl.normalize])
def test_carriage_returns_and_trailing_blanks_leave_every_line(normalize) -> None:
    raw = "\n\nCREATE TABLE t (  \r\n  a INT\t\r\n\n);\r\n\n"

    assert normalize(raw) == "CREATE TABLE t (\n  a INT\n\n);\n"


@pytest.mark.parametrize("normalize", [redshift_ddl.normalize, bigquery_ddl.normalize])
def test_nothing_but_whitespace_normalizes_to_nothing(normalize) -> None:
    assert normalize(" \n\t\n") == ""
