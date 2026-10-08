"""A JSON document is descended through `[keys]`, its members and `[*]` (SPEC 2.2.18)."""

from __future__ import annotations

import json
from pathlib import Path
from types import ModuleType
from typing import Any

import duckdb
import pytest
import yaml

from dbprint.adapters import BigqueryAdapter, ClickhouseAdapter, DatabricksAdapter, PostgresAdapter
from dbprint.adapters.base import PartSource
from dbprint.adapters.dialect import Vendor
from dbprint.adapters.duckdb import DuckdbAdapter
from dbprint.adapters.sql_layout import select_from
from dbprint.config import StatisticsConfig
from dbprint.config.project import ConnectionConfig, RedactRule
from dbprint.conformance import validate_print
from dbprint.engine import Engine
from dbprint.engine import diff as diff_module
from tests._engine_run import conformance_errors
from tests.adapters._composites import generate, parts_of, psql, snowflake
from tests.adapters._credentials import DATABRICKS_CREDS
from tests.adapters._dialects import STATS_MODULES, foreign_fragments
from tests.adapters._sql_style import alias_violations, layout_violations, violations


_SALT = "document-test-salt"
_EVENTS = (
    (
        '{"status":"shipped","buyer":{"id":7,"mail":"ana@example.invalid"},"amt":1.5,'
        '"items":[{"sku":"A1","qty":2},{"sku":"B2"}]}'
    ),
    '{"status":null,"buyer":{"id":"8"},"user-id":3,"items":[]}',
    '{"status":"new","amt":2}',
    "[1,2,3]",
    '"hello"',
    "null",
    None,
)


class TestDuckdb:
    def test_the_column_publishes_its_type_mix_size_and_parts(self, tmp_path: Path) -> None:
        payload = _duckdb_events(tmp_path)

        assert payload["classification"] == "json"
        assert payload["null_count"] == 1
        assert payload["types"] == {"OBJECT": 3, "ARRAY": 1, "NULL": 1, "VARCHAR": 1}
        assert (payload["size"]["min"], payload["size"]["max"]) == (2, 4)
        assert payload["parts_found"] == 14
        assert payload["cardinality"] == 6

    def test_keys_members_and_elements_carry_their_own_populations(self, tmp_path: Path) -> None:
        parts = _duckdb_events(tmp_path)["parts"]

        assert parts["[keys]"]["occurrences"] == 10
        assert parts["[keys]"]["null_count"] == 0
        assert parts["[keys]"]["values"] == [
            {"value": "status", "count": 3},
            {"value": "amt", "count": 2},
            {"value": "buyer", "count": 2},
            {"value": "items", "count": 2},
            {"value": "user-id", "count": 1},
        ]
        assert (parts[".status"]["occurrences"], parts[".status"]["null_count"]) == (3, 1)
        assert parts[".status"]["sql_type"] == "VARCHAR"
        assert parts['["user-id"]']["sql_type"] == "UBIGINT"
        assert parts[".amt"]["sql_type"] == "DOUBLE"
        assert [v["value"] for v in parts[".amt"]["values"]] == [1.5, 2.0]
        assert parts["[*]"]["occurrences"] == 3
        assert parts["[*]"]["sql_type"] == "UBIGINT"

    def test_a_member_of_several_types_stays_a_document_with_its_own_mix(
        self,
        tmp_path: Path,
    ) -> None:
        parts = _duckdb_events(tmp_path)["parts"]

        assert parts[".buyer.id"]["sql_type"] == "JSON"
        assert parts[".buyer.id"]["classification"] == "json"
        assert parts[".buyer.id"]["types"] == {"UBIGINT": 1, "VARCHAR": 1}
        assert "values" not in parts[".buyer.id"]
        assert parts[".buyer"]["types"] == {"OBJECT": 2}
        assert (parts[".buyer"]["size"]["min"], parts[".buyer"]["size"]["max"]) == (1, 2)
        assert parts[".items"]["classification"] == "composite"
        assert parts[".items"]["empty_count"] == 1
        assert parts[".items[*]"]["occurrences"] == 2
        assert parts[".items[*].qty"]["occurrences"] == 1

    def test_the_cap_keeps_the_key_set_and_counts_every_part(self, tmp_path: Path) -> None:
        payload = _duckdb_events(tmp_path, statistics=StatisticsConfig(max_parts=5))

        assert len(payload["parts"]) == 5
        assert "[keys]" in payload["parts"]
        assert payload["parts_found"] == 14

    def test_descent_off_still_publishes_the_type_mix(self, tmp_path: Path) -> None:
        payload = _duckdb_events(tmp_path, statistics=StatisticsConfig(max_parts=0))

        assert payload["classification"] == "json"
        assert payload["types"]["OBJECT"] == 3
        assert "parts" not in payload

    def test_an_all_null_column_publishes_no_type_mix(self, tmp_path: Path) -> None:
        payload = _duckdb_documents(tmp_path, [None, None])

        assert payload["null_count"] == 2
        assert "types" not in payload

    def test_a_zero_row_table_publishes_no_type_mix(self, tmp_path: Path) -> None:
        assert "types" not in _duckdb_documents(tmp_path, [])

    def test_a_prose_member_withholds_its_value_list(self, tmp_path: Path) -> None:
        sentences = (
            "The packet arrived damp and was dried for two days before sowing. ",
            "Germination was slow in the cold frame, so the tray moved indoors. ",
            "Several seedlings damped off after watering; the rest were potted on. ",
        )
        rows = [
            json.dumps(
                {
                    "notes": f"Batch {i}: {sentences[i % 3]}{sentences[(i + 1) % 3]}",
                    "grade": "abcd"[i % 4],
                },
            )
            for i in range(60)
        ]
        parts = _duckdb_documents(tmp_path, list(rows))["parts"]

        assert parts[".notes"]["inferred"]["looks_like"] == "prose"
        assert {"values", "values_coverage", "distribution", "unmeasured"} & set(
            parts[".notes"],
        ) == set()
        assert parts[".grade"]["values_coverage"] == 1.0  # noqa: RUF069 - the expected value is an exact literal

    def test_a_column_rule_withholds_the_key_names_and_marks_the_elements(
        self,
        tmp_path: Path,
    ) -> None:
        payload = _duckdb_events(
            tmp_path,
            redact=(RedactRule(columns=("*.order_event.payload",), with_="mask"),),
        )

        assert set(payload["parts"]) == {"[keys]", "[*]"}
        assert payload["parts"]["[keys]"]["redacted"] == "mask"
        assert payload["parts"]["[*]"]["redacted"] == "mask"
        assert payload["parts_found"] == 14

    def test_keys_and_values_that_are_emails_are_caught(self, tmp_path: Path) -> None:
        rows = [f'{{"contact_by": {{"g{i}@example.invalid": {i}}}}}' for i in range(30)]
        payload = _duckdb_documents(
            tmp_path,
            [*rows, *_EVENTS[:3]],
            redact=(RedactRule(looks_like=("email",), with_="hash"),),
        )
        parts = payload["parts"]

        assert parts[".contact_by[keys]"]["inferred"]["looks_like"] == "email"
        assert parts[".contact_by[keys]"]["redacted"] == "hash"
        assert not [p for p in parts if p.startswith('.contact_by["')]
        assert not [
            p for p in (tmp_path / "prints").rglob("*.yaml") if "@example.invalid" in p.read_text()
        ]

    def test_an_unredacted_email_member_warns(self, tmp_path: Path) -> None:
        _duckdb_events(tmp_path)
        issues = validate_print(tmp_path / "prints" / "garden")

        assert any(
            i.code == "privacy.unredacted-sensitive" and ".buyer.mail" in i.path for i in issues
        )


def test_a_postgres_document_reads_its_own_type_names(
    postgres_test_db: dict[str, str],
    tmp_path: Path,
) -> None:
    with psql(postgres_test_db) as conn:
        conn.execute("CREATE TABLE public.order_event (event_no integer, payload jsonb, raw json)")

        for number, event in enumerate(_EVENTS):
            conn.execute(
                "INSERT INTO public.order_event VALUES (%s, %s, %s)",
                (number, event, '{"a": 1, "a": 2}' if number == 0 else None),
            )

    columns = generate(PostgresAdapter(postgres_test_db), "postgres", tmp_path, "*.order_event")
    payload = columns["payload"]

    assert payload["types"] == {"object": 3, "array": 1, "null": 1, "string": 1}
    assert payload["parts"][".status"]["sql_type"] == "string"
    assert payload["parts"][".buyer.id"]["sql_type"] == "jsonb"
    assert payload["parts"][".buyer.id"]["types"] == {"number": 1, "string": 1}
    assert payload["parts"][".amt"]["sql_type"] == "number"
    assert payload["parts_found"] == 14
    assert columns["raw"]["parts"]["[keys]"]["occurrences"] == 1


def test_postgres_profiles_a_jsonpath_as_text_and_keeps_xml_declined(
    postgres_test_db: dict[str, str],
    tmp_path: Path,
) -> None:
    with psql(postgres_test_db) as conn:
        conn.execute("CREATE TABLE public.rule (rule_no integer, expr jsonpath, body xml)")
        conn.execute(
            "INSERT INTO public.rule VALUES (1, '$.a ? (@ > 1)', '<a/>'), (2, '$.b', '<b/>')",
        )

    columns = generate(PostgresAdapter(postgres_test_db), "postgres", tmp_path, "*.rule")

    assert columns["expr"]["classification"] == "categorical"
    assert {v["value"] for v in columns["expr"]["values"]} == {'$."a"?(@ > 1)', '$."b"'}
    assert columns["body"]["classification"] == "unsupported"


def test_a_clickhouse_document_treats_a_null_as_absent(
    clickhouse_native_connection: Any,
    tmp_path: Path,
) -> None:
    cursor = clickhouse_native_connection
    cursor.execute(
        "CREATE TABLE seedbank.order_event (event_no UInt32, payload JSON) ENGINE = Memory",
    )
    cursor.execute(
        'INSERT INTO seedbank.order_event VALUES (1, \'{"a": null, "b": 1}\'), (2, \'{"b": 2}\'), '
        '(3, \'{"b": "x", "c": {"d": [1, 2]}}\')',
    )
    adapter = ClickhouseAdapter(
        {"host": "chdb", "database": "seedbank"},
        cursor_factory=lambda _params: cursor,
    )
    payload = generate(adapter, "clickhouse", tmp_path, "*.order_event")["payload"]

    assert payload["types"] == {"Object": 3}
    assert ".a" not in payload["parts"]
    assert payload["parts"][".b"]["occurrences"] == 3
    assert payload["parts"][".b"]["sql_type"] == "JSON"
    assert payload["parts"][".c.d"]["classification"] == "composite"


def test_a_bigquery_document_is_descended(bigquery_test_dataset: Any) -> None:
    cursor, dataset = bigquery_test_dataset
    cursor.execute(f"CREATE TABLE `{dataset}`.order_event (event_no INT64, payload JSON)")
    cursor.execute(
        f"INSERT INTO `{dataset}`.order_event (event_no, payload) VALUES "
        """(1, JSON '{"status": "new", "user-id": 3}'), (2, JSON '{"status": "old"}')""",
    )
    adapter = BigqueryAdapter(
        {"project": "dbprint-test", "dataset": dataset},
        cursor_factory=lambda _params: cursor,
    )
    parts = {p.path: p for p in parts_of(adapter, "order_event", "payload").parts}

    assert parts["[keys]"].occurrences == 3
    assert parts[".status"].stats.sql_type == "string"
    assert parts['["user-id"]'].occurrences == 1


def test_a_databricks_variant_is_descended(databricks_test_schema: Any) -> None:
    cursor = databricks_test_schema
    cursor.execute("CREATE TABLE order_event (event_no INT, payload VARIANT) USING DELTA")
    cursor.execute(
        'INSERT INTO order_event SELECT 1, parse_json(\'{"status": "new", "amt": 1.5}\') '
        'UNION ALL SELECT 2, parse_json(\'{"status": null, "amt": 2}\') '
        "UNION ALL SELECT 3, parse_json('[1, 2]')",
    )
    adapter = DatabricksAdapter(DATABRICKS_CREDS, cursor_factory=lambda _params: cursor)
    parts = {p.path: p for p in parts_of(adapter, "order_event", "payload").parts}

    assert (parts[".status"].occurrences, parts[".status"].stats.null_count) == (2, 1)
    assert parts[".status"].stats.sql_type == "STRING"
    assert parts["[*]"].occurrences == 2


def test_a_snowflake_variant_and_object_are_descended() -> None:
    setup = (
        "CREATE TABLE seedbank.order_event (event_no INTEGER, payload JSON, attrs JSON)",
        """INSERT INTO seedbank.order_event VALUES
        (1, '{"status": "new", "lines": [{"sku": "a"}]}', '{"colour": "red"}'),
        (2, '{"status": null}', '{"colour": "blue", "size": 3}'),
        (3, '[1, 2]', NULL)""",
    )
    payload = parts_of(snowflake(*setup), "order_event", "payload").parts
    attrs = parts_of(
        snowflake(*setup, column_types={"attrs": "OBJECT"}),
        "order_event",
        "attrs",
    ).parts
    payload, attrs = ({p.path: p for p in parts} for parts in (payload, attrs))

    assert (payload[".status"].occurrences, payload[".status"].stats.null_count) == (2, 1)
    assert payload[".status"].stats.sql_type == "VARCHAR"
    assert payload[".lines[*].sku"].occurrences == 1
    assert payload["[*]"].occurrences == 2
    assert (attrs[".colour"].occurrences, attrs[".size"].occurrences) == (2, 1)


@pytest.mark.parametrize(
    ("vendor", "sql_type"),
    [
        ("duckdb", "JSON"),
        ("postgres", "jsonb"),
        ("mysql", "json"),
        ("snowflake", "VARIANT"),
        ("bigquery", "JSON"),
        ("redshift", "super"),
        ("databricks", "variant"),
        ("clickhouse", "JSON"),
    ],
)
def test_the_document_reads_speak_their_own_dialect(vendor: Vendor, sql_type: str) -> None:
    module: ModuleType = STATS_MODULES[vendor]
    documents = module.DOCUMENTS
    node = PartSource("", sql_type, "src_table src", "src.payload")
    statements = [
        select_from(["ent.k AS v"], documents.entries(node)),
        select_from(["ent.v AS v"], documents.elements(node)),
        select_from([f"{documents.size('src.payload')} AS sz"], "src_table src"),
        select_from([f"{documents.type_of('src.payload')} AS t"], "src_table src"),
    ]

    for statement in statements:
        assert foreign_fragments(statement, vendor) == []
        assert violations(statement, vendor) + alias_violations(statement, vendor) == []
        assert layout_violations(statement, vendor) == []


def _duckdb_events(
    tmp_path: Path,
    statistics: StatisticsConfig | None = None,
    redact: tuple[RedactRule, ...] = (),
) -> dict[str, Any]:
    return _duckdb_documents(tmp_path, list(_EVENTS), statistics=statistics, redact=redact)


def _duckdb_documents(
    tmp_path: Path,
    rows: list[str | None],
    statistics: StatisticsConfig | None = None,
    redact: tuple[RedactRule, ...] = (),
) -> dict[str, Any]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    database = tmp_path / "garden.duckdb"
    con = duckdb.connect(str(database))
    con.execute("CREATE TABLE order_event (event_no INTEGER, payload JSON)")

    for number, row in enumerate(rows):
        con.execute("INSERT INTO order_event VALUES (?, ?)", [number, row])

    con.close()
    conn = ConnectionConfig(
        name="garden",
        adapter="duckdb",
        output=tmp_path / "prints",
        redact=redact,
        redaction_salt=_SALT,
        statistics=statistics or StatisticsConfig(),
    )
    Engine(DuckdbAdapter({"database": str(database)}), conn, tmp_path).generate()
    errors = conformance_errors(tmp_path / "prints" / "garden")

    assert errors == [], errors

    return yaml.safe_load(next((tmp_path / "prints").rglob("statistics.yaml")).read_text())[
        "columns"
    ]["payload"]


def test_a_member_changing_type_diffs_its_type_and_classification(tmp_path: Path) -> None:
    before = _duckdb_documents(tmp_path / "before", ['{"status": "a"}', '{"status": "b"}'])
    after = _duckdb_documents(tmp_path / "after", ['{"status": "a"}', '{"status": 2}'])
    states = {
        "payload": diff_module.ColumnState(
            name="payload",
            sql_type="JSON",
            nullable=True,
            default=None,
        ),
    }
    events = diff_module._diff_statistics(
        "main.order_event",
        diff_module.comparable_columns({"payload": before}),
        diff_module.comparable_columns({"payload": after}),
        states,
        states,
        scoped=False,
    )

    assert {(e.get("part"), e["stat"]) for e in events} >= {
        (".status", "sql_type"),
        (".status", "classification"),
    }
