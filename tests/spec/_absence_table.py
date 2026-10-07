"""The field names SPEC 2.2.7's absence table lists, read from the specification itself."""

from __future__ import annotations

import re

from tests.spec._spec_markdown import section as _section
from tests.spec._spec_markdown import table_rows as _table_rows


_BACKTICKED = re.compile(r"`([^`]+)`")


def absence_table_fields() -> list[str]:
    """Every field named in the first column of SPEC 7.2, in document order."""

    rows = _table_rows(_section("### 7.2 Absent per-column fields", "### 7.3"))

    return [field for cells in rows[1:] for field in _BACKTICKED.findall(cells[0])]
