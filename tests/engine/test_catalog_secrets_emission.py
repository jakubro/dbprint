"""Every place a print repeats catalog text carries SPEC 2.1.3's credential masks."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import yaml

from dbprint.adapters import (
    ColumnStats,
    CommentsMeta,
    Length,
    MockAdapter,
    MockTable,
    PhysicalLayout,
    PhysicalLayoutKey,
    ValueCount,
)
from dbprint.config.project import RedactRule
from dbprint.engine import Engine, GenerateRequest, orchestrator
from tests._curator import conn_config
from tests._engine_run import artifact, conformance_errors
from tests._prints import columns, exact_stats, mock_table
from tests._secrets import FORMATS, jwt


_DEFAULT_PW = "Df4qWz8nRt"
_LABEL_PW = "Lb7xKm3pVs"
_COMMENT_KEY = "Ck2rNw9qZe"
_COMMENT_TOKEN = "Ct6yHb1sMu"
_LAYOUT_TOKEN = "Lt3gJd5vXa"
_URL_PW = "Up8cFe2wQi"
_CHECK_KEY = FORMATS["sk_live"]()
_BEARER = jwt()
_PLANTED = (
    _DEFAULT_PW,
    _LABEL_PW,
    _COMMENT_KEY,
    _COMMENT_TOKEN,
    _LAYOUT_TOKEN,
    _URL_PW,
    _CHECK_KEY,
    _BEARER,
)

_PROBE = "fixture.credential_probe"
_VIEW = "fixture.credential_probe_v"
_RANK = f"enum('pending', 'password={_LABEL_PW}')"


def _fixture(table_comment: str = f"api_key={_COMMENT_KEY}") -> dict[str, MockTable]:
    table_columns = columns(("id", "integer"), ("note", "text"), ("code", "text"), ("rank", _RANK))
    note = replace(table_columns[1], default=f"'password={_DEFAULT_PW}'::text")
    ddl = (
        f"CREATE TABLE {_PROBE} (\n"
        "    id integer NOT NULL,\n"
        f"    note text DEFAULT 'password={_DEFAULT_PW}'::text,\n"
        "    code text,\n"
        f"    rank {_RANK},\n"
        f"    CONSTRAINT code_check CHECK ((code <> '{_CHECK_KEY}'::text))\n"
        ");\n"
    )
    view_ddl = (
        f"CREATE VIEW {_VIEW} AS\n"
        f" SELECT 'https://exporter:{_URL_PW}@host.example/x' AS src,\n"
        f"    'Bearer {_BEARER}' AS auth_header\n"
        f"   FROM {_PROBE};\n"
    )

    return {
        _PROBE: mock_table(
            _PROBE,
            (table_columns[0], note, *table_columns[2:]),
            {
                "id": _categorical("integer", ["1", "2", "3", "4"]),
                "note": _categorical("text", ["n", "n", "n", "n"]),
                "code": _categorical("text", ["c1", "c2", "c3", "c4"]),
                "rank": _categorical(_RANK, ["pending", "pending", "sown", "sown"]),
            },
            ddl=ddl,
            comments=CommentsMeta(
                table=table_comment,
                columns={"note": f"token: {_COMMENT_TOKEN}"},
            ),
            physical_layout=PhysicalLayout(
                mechanism="partition",
                keys=(PhysicalLayoutKey(expression=f"hash(code, 'token={_LAYOUT_TOKEN}')"),),
            ),
            row_count=4,
        ),
        _VIEW: mock_table(
            _VIEW,
            columns(("src", "text"), ("auth_header", "text")),
            {},
            type="view",
            ddl=view_ddl,
        ),
    }


def _categorical(sql_type: str, rows: list[str]) -> ColumnStats:
    counts = {value: rows.count(value) for value in dict.fromkeys(rows)}
    widths = [len(value) for value in rows]
    text = sql_type != "integer"

    return exact_stats(
        sql_type,
        len(counts),
        len(counts) / len(rows),
        values=tuple(ValueCount(value=v, count=n) for v, n in counts.items()),
        values_coverage=1.0,
        distribution="uniform",
        length=(
            Length(min=min(widths), max=max(widths), avg=sum(widths) / len(rows), p95=max(widths))
            if text
            else None
        ),
    )


def _generate(tmp_path: Path, fixture: dict[str, MockTable], **overrides: Any) -> Path:
    Engine(MockAdapter(fixture), conn_config(tmp_path, **overrides), tmp_path).generate(
        GenerateRequest(force=True),
    )

    return tmp_path / "primary"


def _live_states(tmp_path: Path, fixture: dict[str, MockTable]) -> dict[str, Any]:
    with patch.object(
        orchestrator.diff_module,
        "compute",
        wraps=orchestrator.diff_module.compute,
    ) as compute:
        _generate(tmp_path, fixture)

    return compute.call_args.args[1]


class TestTheWrittenPrint:
    def test_no_file_holds_a_planted_secret(self, tmp_path: Path) -> None:
        root = _generate(tmp_path, _fixture())
        written = [p for p in root.rglob("*") if p.is_file()]

        assert [
            (p.name, s) for p in written for s in _PLANTED if s in p.read_text(encoding="utf-8")
        ] == []

    def test_the_mask_stands_where_each_secret_was(self, tmp_path: Path) -> None:
        root = _generate(tmp_path, _fixture())
        ddl = (root / "fixture" / "credential_probe" / "ddl.sql").read_text(encoding="utf-8")
        view = (root / "fixture" / "credential_probe_v" / "ddl.sql").read_text(encoding="utf-8")
        statistics = artifact(root, _PROBE)

        assert "note text DEFAULT 'password=[HIDDEN]'::text," in ddl
        assert "CHECK ((code <> '[HIDDEN]'::text))" in ddl
        assert statistics["columns"]["rank"]["sql_type"] == "enum('pending', 'password=[HIDDEN]')"
        assert statistics["physical_layout"]["keys"] == [
            {"expression": "hash(code, 'token=[HIDDEN]')"},
        ]
        assert "'https://exporter:[HIDDEN]@host.example/x' AS src" in view
        assert "'Bearer [HIDDEN]' AS auth_header" in view

    def test_a_second_run_reports_no_type_or_layout_change(self, tmp_path: Path) -> None:
        _generate(tmp_path, _fixture())
        root = _generate(tmp_path, _fixture())
        kinds = {
            change["kind"]
            for change in yaml.safe_load((root / "diff.yaml").read_text(encoding="utf-8"))[
                "changes"
            ]
        }

        assert kinds.isdisjoint({"column_type_changed", "physical_layout_changed"})

    def test_the_print_is_conformant(self, tmp_path: Path) -> None:
        assert conformance_errors(_generate(tmp_path, _fixture())) == []

    def test_a_redact_rule_leaves_the_ddl_as_no_rule_does(self, tmp_path: Path) -> None:
        plain = _generate(tmp_path / "plain", _fixture())
        rule = RedactRule(columns=("*",), with_="mask")
        conn = conn_config(tmp_path / "redacted")
        redacted = _generate(
            tmp_path / "redacted",
            _fixture(),
            profile=replace(conn.profile, redact=(rule,)),
        )

        assert {p.relative_to(plain): p.read_bytes() for p in plain.rglob("ddl.sql")} == {
            p.relative_to(redacted): p.read_bytes() for p in redacted.rglob("ddl.sql")
        }


class TestTheStateTheDiffCompares:
    def test_comments_defaults_and_types_are_masked(self, tmp_path: Path) -> None:
        state = _live_states(tmp_path, _fixture())[_PROBE]

        assert state.table_comment == "api_key=[HIDDEN]"
        assert state.column_comments == {"note": "token: [HIDDEN]"}
        assert state.columns["note"].default == "'password=[HIDDEN]'::text"
        assert state.columns["rank"].sql_type == "enum('pending', 'password=[HIDDEN]')"

    @pytest.mark.parametrize(
        ("comment", "published"),
        [
            pytest.param(
                'wrapper "mysql", user "u", password "p"',
                'wrapper "mysql", user "u", password "[HIDDEN]"',
                id="connection-string-comment",
            ),
            pytest.param("user:pw@x", "user:pw@x", id="no-url"),
        ],
    )
    def test_a_table_comment(self, tmp_path: Path, comment: str, published: str) -> None:
        assert _live_states(tmp_path, _fixture(comment))[_PROBE].table_comment == published
