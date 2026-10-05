"""Hand-written SQL - fixtures, seed scripts and the adapter pages' `sql` fences - keeps the house style.

The statements adapters emit are checked off the dialect sweep (`test_dialect_guard.py`).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.adapters._sql_style import (
    file_layout_violations,
    file_violations,
    layout_violations,
    violations,
)


_LIB = Path(__file__).resolve().parents[2]
_FENCE_RE = re.compile(r"```sql\n(.*?)```", re.DOTALL)
_PLACEHOLDER_RE = re.compile(r"<[a-z_]+>")


def _files_written_by_hand() -> list[tuple[Path, str]]:
    files = [(path, "postgres") for path in sorted((_LIB / "scripts/sql").glob("*.sql"))]
    files += [
        (path, "postgres") for path in sorted((_LIB / "tests/integration/fixtures").glob("*.sql"))
    ]
    files += [
        (path, "mysql") for path in sorted((_LIB / "tests/adapters/fixtures/mysql").glob("*.sql"))
    ]

    for vendor_dir in sorted((_LIB / "tests/live/fixtures").iterdir()):
        files += [(path, vendor_dir.name) for path in sorted(vendor_dir.glob("*.sql"))]

    return files


def _fences() -> list[tuple[str, str, str]]:
    return [
        (f"{page.name}#{index}", page.stem, fence)
        for page in sorted((_LIB / "docs/adapters").glob("*.md"))
        for index, fence in enumerate(_FENCE_RE.findall(page.read_text(encoding="utf-8")))
    ]


@pytest.mark.parametrize(
    ("path", "dialect"),
    _files_written_by_hand(),
    ids=lambda value: value.relative_to(_LIB).as_posix() if isinstance(value, Path) else value,
)
def test_a_sql_file_written_by_hand_keeps_the_house_style(path: Path, dialect: str) -> None:
    text = path.read_text(encoding="utf-8")

    assert file_violations(text, dialect) + file_layout_violations(text, dialect) == []


@pytest.mark.parametrize(("name", "dialect", "fence"), _fences(), ids=lambda value: value[:40])
def test_an_adapter_pages_sql_fence_keeps_the_house_style(
    name: str,
    dialect: str,
    fence: str,
) -> None:
    placeholders = bool(_PLACEHOLDER_RE.search(fence))
    text = _PLACEHOLDER_RE.sub("x", fence)

    assert file_violations(text, dialect, casing_only=placeholders) == [], name


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "INSERT INTO t (a) SELECT i FROM GENERATE_SERIES(1, 2) AS i;",
            ["AS before the alias 'i'", "unqualified column 'i'"],
        ),
        ("INSERT INTO t (a) SELECT g.i FROM GENERATE_SERIES(1, 2) g (i);", []),
        ("INSERT INTO t VALUES (1);", []),
        ("CREATE VIEW v AS SELECT a FROM t;", []),
    ],
)
def test_a_file_holds_dml_to_every_rule_and_ddl_to_casing(text: str, expected: list[str]) -> None:
    assert file_violations(text, "postgres") == expected


class TestTheCheckerFlagsWhatItExistsToCatch:
    @pytest.mark.parametrize(
        ("sql", "dialect", "expected"),
        [
            ("SELECT s.a FROM t s WHERE s.a IS NOT NULL", "postgres", []),
            ("select s.a FROM t s", "postgres", ["line 1: lowercase 'select'"]),
            (
                "SELECT date_trunc('day', s.a) FROM t s",
                "postgres",
                ["line 1: lowercase 'date_trunc'"],
            ),
            ("SELECT CAST(s.a AS text) FROM t s", "postgres", ["line 1: lowercase 'text'"]),
            ("SELECT uniqExact(s.a), toString(s.b), any(s.c) FROM t s", "clickhouse", []),
            ("select s.a FROM t s", "clickhouse", ["line 1: lowercase 'select'"]),
            (
                "SELECT if(empty(groupArray(s.a) AS g), NULL, g[1]) FROM t s",
                "clickhouse",
                [],
            ),
            ("SELECT s.a AS b FROM t s WHERE b > 0", "postgres", ["unqualified column 'b'"]),
            ("SET spark.sql.session.collation.default", "databricks", []),
            (
                "SELECT CAST(s.a AS character varying) FROM t s",
                "postgres",
                ["line 1: lowercase 'character varying'"],
            ),
            ("SELECT s.a FROM t AS s", "postgres", ["AS before the alias 's'"]),
            ("SELECT t.a FROM t", "postgres", ["unaliased 't'"]),
            ("SELECT a FROM t s", "postgres", ["unqualified column 'a'"]),
            ("SELECT COUNT(*) FROM t s", "postgres", ["COUNT(*), not COUNT(1)"]),
            (
                "SELECT SUM(CASE WHEN s.a IS NOT NULL THEN 1 ELSE 0 END) FROM t s",
                "postgres",
                ["SUM(CASE WHEN ... IS NOT NULL ...), not COUNT(col)"],
            ),
            (
                "SELECT a.x FROM p a INNER JOIN q b ON b.y = a.x",
                "postgres",
                ["INNER JOIN, not JOIN"],
            ),
            (
                "SELECT a.x FROM p a FULL OUTER JOIN q b ON b.y = a.x",
                "postgres",
                ["FULL OUTER JOIN, not FULL JOIN"],
            ),
            ("SELECT s.a AS cnt FROM t s ORDER BY cnt", "postgres", []),
            ("SELECT s.a FROM (SELECT * FROM x.t WHERE t.a > 0) s", "postgres", []),
            ("WITH agg AS (SELECT MIN(s.a) AS mn FROM t s) SELECT mn FROM agg", "postgres", []),
            ("SELECT s.a FROM (SELECT 1 AS a) AS s", "postgres", ["AS before the alias 's'"]),
        ],
    )
    def test_one_statement(self, sql: str, dialect: str, expected: list[str]) -> None:
        assert violations(sql, dialect) == expected


class TestTheLayoutCheckFlagsWhatItExistsToCatch:
    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT DB_COLLATION()",
            'SELECT COUNT(1) FROM "s"."t" src',
            "SELECT DISTINCT\n  src.id\nFROM\n  s.t src\nWHERE\n  src.id IS NOT NULL",
            "SELECT\n  src.a,\n  src.b\nFROM\n  s.t src\nWHERE\n  src.a > 0\n  AND src.b > 0\nORDER BY\n  src.a\nLIMIT 5",
            "SELECT\n  EXTRACT(YEAR FROM src.a),\n  COUNT(1) OVER (\n    PARTITION BY src.b ORDER BY src.a\n  )\nFROM\n  s.t src",
            "SELECT\n  CASE WHEN src.a IS NULL THEN 0 ELSE 1 END AS flag,\n  src.b\nFROM\n  s.t src",
            "SELECT\n  ARRAY['a', 'b'] AS tags,\n  src.b\nFROM\n  s.t src",
            "SELECT\n  src.a\nFROM\n  s.t src\nWHERE\n  src.b IS DISTINCT FROM NULL",
            "SELECT\n  CASE\n    WHEN src.a < 0 THEN 'neg'\n    WHEN src.a > 0 THEN 'pos'\n    ELSE 'zero'\n  END AS sign\nFROM\n  s.t src",
            "SELECT\n  cls.relname\nFROM\n  pg_class cls\n  JOIN pg_namespace nsp ON nsp.oid = cls.relnamespace",
            "SELECT\n  COALESCE(\n    src.a,\n    0\n  ) AS a\nFROM\n  s.t src",
            "SELECT\n  src.a\nFROM\n  (\n    SELECT\n      inr.a\n    FROM\n      s.t inr\n    WHERE\n      inr.a > 0\n  ) src",
        ],
    )
    def test_a_statement_in_the_house_layout(self, sql: str) -> None:
        assert layout_violations(sql, "postgres") == []

    @pytest.mark.parametrize(
        ("sql", "expected"),
        [
            (
                "SELECT\n  src.a\nFROM s.t src\nWHERE\n  src.a > 0",
                ["line 3: FROM body on its keyword's line"],
            ),
            (
                "SELECT\n  src.a\nFROM\n  s.t src\nWHERE src.a > 0\n  AND src.b > 0",
                ["line 5: WHERE body on its keyword's line"],
            ),
            (
                "SELECT src.a\nFROM\n  s.t src",
                ["line 1: SELECT body on its keyword's line"],
            ),
            (
                "SELECT\n  src.a\nFROM\n  s.t src\nORDER BY src.a",
                ["line 5: ORDER BY body on its keyword's line"],
            ),
            (
                "SELECT\n  src.a\nFROM\n  s.t src WHERE\n  src.a > 0",
                ["line 4: WHERE mid-line"],
            ),
            (
                "SELECT\n  src.a,  src.b\nFROM\n  s.t src",
                ["line 2: two select-list items on one line"],
            ),
            (
                "SELECT\n  cls.relname\nFROM\n  pg_class cls LEFT JOIN pg_namespace nsp ON nsp.oid = cls.relnamespace",
                ["line 4: a join mid-line"],
            ),
            (
                "SELECT\n  COALESCE(src.a,\n    0) AS a\nFROM\n  s.t src",
                [
                    "line 2: an argument on the opening or closing line of a call that spans lines",
                    "line 3: an argument on the opening or closing line of a call that spans lines",
                ],
            ),
            (
                "SELECT\n  ARRAY[\n    'a', 'b',\n    'c'\n  ] AS tags\nFROM\n  s.t src",
                ["line 3: two arguments on one line of a call that spans lines"],
            ),
            (
                "SELECT\n  CASE WHEN src.a < 0 THEN 'neg'\n    WHEN src.a > 0 THEN 'pos' ELSE 'zero' END AS sign\nFROM\n  s.t src",
                [
                    "line 2: WHEN of a multi-branch CASE mid-line",
                    "line 3: ELSE of a multi-branch CASE mid-line",
                    "line 3: END of a multi-branch CASE mid-line",
                ],
            ),
            (
                "SELECT\n  src.a\nFROM\n  (SELECT inr.a FROM s.t inr) src",
                [
                    "line 4: SELECT mid-line",
                    "line 4: FROM mid-line",
                    "line 4: SELECT body on its keyword's line",
                    "line 4: FROM body on its keyword's line",
                ],
            ),
            (
                "SELECT\n  src.a\nFROM\n  s.t src\nWHERE\n  src.a = '" + "x" * 120 + "'",
                ["line 6: 132 characters, over 120"],
            ),
            (
                'SELECT COUNT(1) FROM "s"."t" src WHERE src.a > 0',
                [
                    "line 1: FROM mid-line",
                    "line 1: WHERE mid-line",
                    "line 1: SELECT body on its keyword's line",
                    "line 1: FROM body on its keyword's line",
                    "line 1: WHERE body on its keyword's line",
                ],
            ),
            (
                "SELECT\n  c.relname\nFROM\n  pg_class c\n  JOIN pg_namespace ns ON ns.oid = c.relnamespace",
                ["alias 'c', not three letters", "alias 'ns', not three letters"],
            ),
            ('SELECT COUNT(1) FROM "s"."t" s', ["alias 's', not three letters"]),
        ],
    )
    def test_a_statement_out_of_layout(self, sql: str, expected: list[str]) -> None:
        assert layout_violations(sql, "postgres") == expected

    def test_line_length_is_read_from_the_unbound_statement(self) -> None:
        bound = "SELECT\n  src.a\nFROM\n  s.t src\nWHERE\n  src.a = '" + "x" * 120 + "'"
        template = "SELECT\n  src.a\nFROM\n  s.t src\nWHERE\n  src.a = %s"

        assert layout_violations(bound, "postgres", template=template) == []

    def test_ddl_keeps_the_casing_rule_alone(self) -> None:
        assert (
            layout_violations(
                "CREATE TABLE t AS SELECT src.a, src.b FROM s.t src WHERE src.a > 0",
                "postgres",
            )
            == []
        )

    def test_a_file_names_the_line_of_each_statement(self) -> None:
        text = "CREATE TABLE t (a INT);\nINSERT INTO t (a)\nSELECT\n  CASE WHEN gen.i < 2 THEN 1\n  WHEN gen.i < 4 THEN 2 END\nFROM\n  GENERATE_SERIES(1, 5) gen (i);\n"

        assert file_layout_violations(text, "postgres") == [
            "line 4: WHEN of a multi-branch CASE mid-line",
            "line 5: END of a multi-branch CASE mid-line",
        ]
