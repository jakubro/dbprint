"""Integration-test fixtures: per-test e2e Postgres seeded with the dbprint demo schema."""

from __future__ import annotations

from collections.abc import Iterator
from typing import LiteralString, cast

import pytest

from tests.conftest import PostgresCluster, fresh_database, pg_connect
from tests.integration.fixtures import DATA_SQL, SCHEMA_SQL


@pytest.fixture
def e2e_postgres_db(postgres_cluster: PostgresCluster) -> Iterator[dict[str, str]]:
    """Create a fresh DB in the shared cluster seeded with the e2e schema + data."""

    with fresh_database(postgres_cluster, "e2e") as creds:
        with pg_connect(creds) as conn:
            # Trusted disk-path SQL; cast satisfies psycopg's LiteralString overload.
            conn.execute(cast(LiteralString, SCHEMA_SQL))
            conn.execute(cast(LiteralString, DATA_SQL))

        yield creds
