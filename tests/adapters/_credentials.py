"""Credentials the in-process substrates accept, shared by the suites that open them."""

from __future__ import annotations


DATABRICKS_CREDS = {
    "server_hostname": "local",
    "http_path": "local",
    "access_token": "local",
    "catalog": "spark_catalog",
}


SNOWFLAKE_CREDS: dict[str, str] = {
    "account": "test-account",
    "user": "test-user",
    "password": "test-password",
    "warehouse": "test-warehouse",
    "database": "memory",
    "role": "test-role",
}
