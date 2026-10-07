"""A spatial column publishes its kinds, reference systems and bounding box, never its values."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import duckdb
import pytest
import yaml

from dbprint.adapters import (
    ClickhouseAdapter,
    ColumnMeta,
    PostgresAdapter,
)
from dbprint.adapters.duckdb import DuckdbAdapter
from dbprint.adapters.duckdb import stats as duckdb_stats
from dbprint.config.project import ConnectionConfig, RedactRule
from dbprint.conformance import validate_print
from dbprint.engine import Engine
from tests._engine_run import conformance_errors
from tests.adapters._composites import generate, psql
from tests.adapters._mysql import build_mysql


_VALUE_FIELDS = ("cardinality", "cardinality_ratio", "values", "sketch", "distribution")


@pytest.mark.usefixtures("duckdb_spatial")
class TestDuckdb:
    def test_a_point_column_describes_its_geometry(self, tmp_path: Path) -> None:
        columns = _duckdb_columns(
            tmp_path,
            "CREATE TABLE place (site_no INTEGER, geom GEOMETRY)",
            "INSERT INTO place SELECT i, ST_Point(i % 37, i % 41) FROM range(1000) r(i)",
        )
        geom = columns["geom"]

        assert geom["classification"] == "spatial"
        assert geom["geometry"] == {
            "kinds": [{"kind": "point", "count": 1000}],
            "srids": [{"srid": 0, "count": 1000}],
            "dimensions": [{"dimensions": "xy", "count": 1000}],
            "empty_count": 0,
            "invalid_count": 0,
        }
        assert geom["extent"] == {"min_x": 0.0, "min_y": 0.0, "max_x": 36.0, "max_y": 40.0}
        assert not set(_VALUE_FIELDS) & set(geom)

    def test_a_typed_crs_is_the_srid_as_the_engine_spells_it(self) -> None:
        # In memory: duckdb 1.5.5 reads a file's GEOMETRY('OGC:CRS84') column back without its CRS.
        con: Any = duckdb.connect()
        con.execute("LOAD spatial")
        con.execute("CREATE TABLE place (geom GEOMETRY('OGC:CRS84'))")
        con.execute("INSERT INTO place SELECT ST_Point(i, i) FROM range(12) r(i)")
        column = ColumnMeta(
            name="geom",
            sql_type="GEOMETRY('OGC:CRS84')",
            nullable=True,
            default=None,
            ordinal=1,
        )

        geometry, _ = duckdb_stats._fetch_spatial(con, "place src", column)

        assert geometry.srids == (("OGC:CRS84", 12),)

    def test_mixed_kinds_dimensions_empties_and_invalid_values_each_sum_to_the_non_nulls(
        self,
        tmp_path: Path,
    ) -> None:
        columns = _duckdb_columns(
            tmp_path,
            "CREATE TABLE plot (outline GEOMETRY)",
            "INSERT INTO plot SELECT ST_Point(i, i) FROM range(5) r(i)",
            "INSERT INTO plot VALUES ('POINT Z (1 2 3)'::GEOMETRY), ('POLYGON EMPTY'::GEOMETRY), "
            "('POLYGON((0 0, 1 1, 1 0, 0 1, 0 0))'::GEOMETRY), (NULL)",
        )
        geometry = columns["outline"]["geometry"]

        assert geometry["kinds"] == [
            {"kind": "point", "count": 6},
            {"kind": "polygon", "count": 2},
        ]
        assert geometry["dimensions"] == [
            {"dimensions": "xy", "count": 7},
            {"dimensions": "xyz", "count": 1},
        ]
        assert geometry["empty_count"] == 1
        assert geometry["invalid_count"] == 1
        assert columns["outline"]["null_count"] == 1

    def test_three_distinct_points_stay_spatial_and_list_no_values(self, tmp_path: Path) -> None:
        columns = _duckdb_columns(
            tmp_path,
            "CREATE TABLE marker (shape GEOMETRY)",
            "INSERT INTO marker SELECT ST_Point(i % 3, 0) FROM range(90) r(i)",
        )
        shape = columns["shape"]

        assert shape["classification"] == "spatial"
        assert "values" not in shape
        assert shape["inferred"] == {"sensitivity": "geolocation"}

    def test_a_marker_withholds_the_extent_and_keeps_the_geometry(self, tmp_path: Path) -> None:
        columns = _duckdb_columns(
            tmp_path,
            "CREATE TABLE marker (shape GEOMETRY)",
            "INSERT INTO marker SELECT ST_Point(i, i) FROM range(20) r(i)",
            redact=(RedactRule(sensitivity=("geolocation",), with_="mask"),),
        )
        shape = columns["shape"]

        assert shape["redacted"] == "mask"
        assert "extent" not in shape
        assert shape["geometry"]["kinds"] == [{"kind": "point", "count": 20}]

    def test_an_unredacted_extent_warns_and_the_check_still_passes(self, tmp_path: Path) -> None:
        _duckdb_columns(
            tmp_path,
            "CREATE TABLE marker (shape GEOMETRY)",
            "INSERT INTO marker SELECT ST_Point(i, i) FROM range(20) r(i)",
        )
        issues = validate_print(tmp_path / "prints" / "garden")

        assert [i for i in issues if i.severity == "error"] == []
        assert any(
            i.code == "privacy.unredacted-sensitive" and "extent" in i.detail for i in issues
        )

    def test_the_extent_is_rounded_outward(self, tmp_path: Path) -> None:
        columns = _duckdb_columns(
            tmp_path,
            "CREATE TABLE marker (shape GEOMETRY)",
            "INSERT INTO marker VALUES (ST_Point(1.0000004, 0)), (ST_Point(-1.0000004, 0))",
        )
        extent = columns["shape"]["extent"]

        assert extent["max_x"] == 1.000001
        assert extent["min_x"] == -1.000001

    def test_without_the_extension_the_column_names_both_fields_unmeasured(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        real = duckdb_stats.exec_query

        def no_extension(cursor: Any, sql: str, *params: Any) -> Any:
            if sql == "LOAD spatial":
                raise duckdb.IOException('Extension "spatial" not found')

            return real(cursor, sql, *params)

        monkeypatch.setattr(duckdb_stats, "exec_query", no_extension)

        with caplog.at_level(logging.WARNING):
            columns = _duckdb_columns(
                tmp_path,
                "CREATE TABLE marker (marker_id INTEGER, shape GEOMETRY)",
                "INSERT INTO marker SELECT i, ST_Point(i, i) FROM range(100) r(i)",
            )

        assert columns["shape"]["classification"] == "spatial"
        assert columns["shape"]["unmeasured"] == ["extent", "geometry"]
        assert columns["marker_id"]["classification"] == "numeric"
        assert any("spatial extension" in r.getMessage() for r in caplog.records)

    def test_a_view_over_a_spatial_column_is_spatial_by_type_alone(self, tmp_path: Path) -> None:
        columns = _duckdb_columns(
            tmp_path,
            "CREATE TABLE place (geom GEOMETRY)",
            "INSERT INTO place SELECT ST_Point(i, i) FROM range(10) r(i)",
            "CREATE VIEW place_view AS SELECT geom FROM place",
            table="place_view",
        )

        assert columns["geom"] == {
            "sql_type": "GEOMETRY",
            "nullable": True,
            "classification": "spatial",
        }


def test_postgis_reads_geometry_and_geography_in_their_own_srid(
    postgis: object,
    postgres_test_db: dict[str, str],
    tmp_path: Path,
) -> None:
    del postgis

    with psql(postgres_test_db) as conn:
        conn.execute("CREATE EXTENSION postgis")
        conn.execute(
            "CREATE TABLE public.site (site_id integer, pin geometry(Point, 3857), "
            "area geography(Polygon, 4326), spot point)",
        )
        conn.execute(
            "INSERT INTO public.site SELECT i, ST_SetSRID(ST_MakePoint(i * 10, i * 20), 3857), "
            "ST_MakeEnvelope(i, i, i + 1, i + 1, 4326)::geography, point(i, i) "
            "FROM generate_series(1, 30) i",
        )

    columns = generate(PostgresAdapter(postgres_test_db), "postgres", tmp_path, "*.site")

    assert columns["pin"]["classification"] == "spatial"
    assert columns["pin"]["geometry"]["srids"] == [{"srid": 3857, "count": 30}]
    assert columns["pin"]["extent"] == {
        "min_x": 10.0,
        "min_y": 20.0,
        "max_x": 300.0,
        "max_y": 600.0,
    }
    assert columns["area"]["geometry"]["kinds"] == [{"kind": "polygon", "count": 30}]
    assert columns["area"]["geometry"]["srids"] == [{"srid": 4326, "count": 30}]
    assert columns["spot"]["classification"] == "unsupported"


def test_mysql_reads_its_spatial_types_with_a_planar_extent(
    mysql_test_db: dict[str, str],
    tmp_path: Path,
) -> None:
    import mysql.connector

    conn = mysql.connector.connect(
        host=mysql_test_db["host"],
        port=int(mysql_test_db["port"]),
        user=mysql_test_db["user"],
        password="",
        database=mysql_test_db["database"],
        autocommit=True,
    )

    try:
        cursor = conn.cursor(buffered=True)
        cursor.execute("CREATE TABLE plot (plot_id INT, corner POINT, edge LINESTRING)")
        cursor.execute(
            "INSERT INTO plot VALUES "
            + ",".join(
                f"({i}, ST_GeomFromText('POINT({i} {i * 2})'), "
                f"ST_GeomFromText('LINESTRING(0 {i}, 5 {i})'))"
                for i in range(1, 41)
            ),
        )
    finally:
        conn.close()

    columns = generate(build_mysql(mysql_test_db), "mysql", tmp_path, "*.plot")

    assert columns["corner"]["classification"] == "spatial"
    assert columns["corner"]["geometry"]["kinds"] == [{"kind": "point", "count": 40}]
    assert columns["corner"]["geometry"]["srids"] == [{"srid": 0, "count": 40}]
    assert "invalid_count" not in columns["corner"]["geometry"]
    assert columns["corner"]["extent"] == {"min_x": 1.0, "min_y": 2.0, "max_x": 40.0, "max_y": 80.0}
    assert columns["edge"]["extent"] == {"min_x": 0.0, "min_y": 1.0, "max_x": 5.0, "max_y": 40.0}


def test_clickhouse_reads_every_geo_type_without_a_reference_system(
    clickhouse_native_connection: Any,
    tmp_path: Path,
) -> None:
    cursor = clickhouse_native_connection
    cursor.execute(
        "CREATE TABLE seedbank.plot (plot_id UInt32, corner Point, edge LineString, "
        "outline Polygon, shape Geometry) ENGINE = Memory",
    )
    cursor.execute(
        "INSERT INTO seedbank.plot SELECT number, (number, number * 2), "
        "[(0, number), (5, number)], [[(0, 0), (number, 0), (0, number), (0, 0)]], "
        "if(number % 2 = 0, (number, 1)::Point, [(number, 0), (number, 3)]::LineString) "
        "FROM numbers(1, 30)",
    )
    adapter = ClickhouseAdapter(
        {"host": "chdb", "database": "seedbank"},
        cursor_factory=lambda _params: cursor,
    )
    columns = generate(adapter, "clickhouse", tmp_path, "*.plot")

    assert columns["corner"]["geometry"] == {
        "kinds": [{"kind": "point", "count": 30}],
        "dimensions": [{"dimensions": "xy", "count": 30}],
        "empty_count": 0,
    }
    assert columns["corner"]["extent"] == {"min_x": 1.0, "min_y": 2.0, "max_x": 30.0, "max_y": 60.0}
    assert columns["outline"]["extent"] == {
        "min_x": 0.0,
        "min_y": 0.0,
        "max_x": 30.0,
        "max_y": 30.0,
    }
    assert columns["shape"]["geometry"]["kinds"] == [
        {"kind": "linestring", "count": 15},
        {"kind": "point", "count": 15},
    ]
    assert columns["shape"]["extent"] == {"min_x": 1.0, "min_y": 0.0, "max_x": 30.0, "max_y": 3.0}


def _duckdb_columns(
    tmp_path: Path,
    *statements: str,
    redact: tuple[RedactRule, ...] = (),
    table: str | None = None,
) -> dict[str, Any]:
    database = tmp_path / "garden.duckdb"
    con = duckdb.connect(str(database))
    con.execute("LOAD spatial")

    for statement in statements:
        con.execute(statement)

    con.close()
    conn = ConnectionConfig(
        name="garden",
        adapter="duckdb",
        output=tmp_path / "prints",
        redact=redact,
    )

    Engine(DuckdbAdapter({"database": str(database)}), conn, tmp_path).generate()
    written = {
        path.parent.name: path for path in (tmp_path / "prints" / "garden").rglob("statistics.yaml")
    }
    path = written[table] if table is not None else next(iter(written.values()))
    errors = conformance_errors(tmp_path / "prints" / "garden")

    assert errors == [], errors

    return yaml.safe_load(path.read_text())["columns"]
