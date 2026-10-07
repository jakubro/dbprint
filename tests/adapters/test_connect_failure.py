"""A server connection that cannot open says where it tried, naming no database it was not given."""

from __future__ import annotations

import pytest

from dbprint.adapters.postgres.connection import (
    Connection,
    ConnectionParams,
    PostgresConnectionError,
)
from dbprint.adapters.redshift.connection import Connection as RedshiftConnection
from dbprint.adapters.redshift.connection import ConnectionParams as RedshiftParams
from dbprint.adapters.redshift.connection import RedshiftConnectionError


def test_a_redshift_session_without_a_database_names_none() -> None:
    params = RedshiftParams.from_credentials({"host": "h", "user": "u", "password": "p"})

    def refuse(_params: RedshiftParams) -> None:
        raise OSError("connection refused")

    connection = RedshiftConnection(params, refuse)

    with pytest.raises(RedshiftConnectionError) as raised:
        connection.open()

    assert str(raised.value) == "could not connect to Redshift at h:5439 as 'u': connection refused"


def test_a_postgres_session_names_the_database_it_was_given() -> None:
    params = ConnectionParams.from_credentials(
        {"host": "127.0.0.1", "port": "1", "user": "u", "password": "", "database": "arboretum"},
    )

    with pytest.raises(PostgresConnectionError, match=r"at 127\.0\.0\.1:1/arboretum as 'u': "):
        Connection(params).open()
