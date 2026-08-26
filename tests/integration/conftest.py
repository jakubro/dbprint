"""Integration-test fixtures: per-test e2e Postgres seeded with the dbprint demo schema."""

from __future__ import annotations

import secrets
from collections.abc import Iterator
from typing import LiteralString, cast

import psycopg
import pytest
from psycopg import sql

from tests.conftest import PostgresCluster
from tests.integration.fixtures import DATA_SQL, SCHEMA_SQL


@pytest.fixture
def e2e_postgres_db(postgres_cluster: PostgresCluster) -> Iterator[dict[str, str]]:
    """Create a fresh DB in the shared cluster seeded with the e2e schema + data."""

    db_name = f"e2e_{secrets.token_hex(4)}"
    admin_creds = {
        "host": "127.0.0.1",
        "port": str(postgres_cluster.port),
        "database": "postgres",
        "user": postgres_cluster.superuser,
        "password": "",
    }
    _exec_admin(admin_creds, sql.SQL("CREATE DATABASE {}").format(sql.Identifier(db_name)))

    db_creds = {**admin_creds, "database": db_name}
    _seed(db_creds)

    try:
        yield db_creds
    finally:
        _exec_admin(
            admin_creds,
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(db_name)),
        )


def _seed(creds: dict[str, str]) -> None:
    with psycopg.connect(
        host=creds["host"],
        port=int(creds["port"]),
        dbname=creds["database"],
        user=creds["user"],
        password=creds["password"],
        autocommit=True,
    ) as conn:
        # Trusted disk-path SQL; cast satisfies psycopg's LiteralString overload.
        conn.execute(cast(LiteralString, SCHEMA_SQL))
        conn.execute(cast(LiteralString, DATA_SQL))


def _exec_admin(creds: dict[str, str], stmt: sql.SQL | sql.Composed) -> None:
    with psycopg.connect(
        host=creds["host"],
        port=int(creds["port"]),
        dbname=creds["database"],
        user=creds["user"],
        password=creds["password"],
        autocommit=True,
    ) as conn:
        conn.execute(stmt)
