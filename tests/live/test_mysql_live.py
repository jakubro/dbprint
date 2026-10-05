"""Live e2e against real Oracle MySQL: extract -> classify -> write, plus the native JSON
type MariaDB's substrate cannot reproduce."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from tests.live import _harness as harness


# Every variable `_live_creds` reads without a default, so exporting exactly what the skip
# reason names produces a run rather than a KeyError inside the first test.
_REQUIRED_ENV = ("MYSQL_HOST", "MYSQL_DATABASE", "MYSQL_USER")

pytestmark = pytest.mark.skipif(
    not all(os.environ.get(name) for name in _REQUIRED_ENV),
    reason=(
        f"set {', '.join(_REQUIRED_ENV)} to run the live MySQL e2e "
        "(MYSQL_PORT defaults to 3306, MYSQL_PASSWORD to empty)"
    ),
)

CONN_NAME = "mysql_live"
_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "mysql"


def _live_creds() -> dict[str, str]:
    return {
        "host": os.environ["MYSQL_HOST"],
        "port": os.environ.get("MYSQL_PORT", "3306"),
        "database": os.environ["MYSQL_DATABASE"],
        "user": os.environ["MYSQL_USER"],
        "password": os.environ.get("MYSQL_PASSWORD", ""),
    }


def _apply_fixtures(creds: dict[str, str]) -> None:
    import mysql.connector

    conn = mysql.connector.connect(
        host=creds["host"],
        port=int(creds["port"]),
        database=creds["database"],
        user=creds["user"],
        password=creds["password"],
        autocommit=True,
    )

    try:
        cursor = conn.cursor()

        for statement in harness.fixture_statements(_FIXTURE_DIR, "mysql"):
            cursor.execute(statement)

        cursor.close()
    finally:
        conn.close()


def _credential_env(creds: dict[str, str]) -> dict[str, str]:
    upper = CONN_NAME.upper()

    return {
        f"DBPRINT_{upper}_HOST": creds["host"],
        f"DBPRINT_{upper}_PORT": str(creds["port"]),
        f"DBPRINT_{upper}_DATABASE": creds["database"],
        f"DBPRINT_{upper}_USER": creds["user"],
        f"DBPRINT_{upper}_PASSWORD": creds["password"] or "",
    }


def test_mysql_live_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Full pipeline against real MySQL: generate, conformance-clean, native JSON."""

    creds = _live_creds()
    _apply_fixtures(creds)

    print_dir = harness.generate(tmp_path, monkeypatch, CONN_NAME, "mysql", _credential_env(creds))

    assert (print_dir / "manifest.yaml").is_file()

    harness.assert_conformant(print_dir)

    db = creds["database"]
    stats = yaml.safe_load((print_dir / db / "herbarium_sheet/statistics.yaml").read_text())
    columns = stats["columns"]
    # Native JSON type classifies as json on Oracle MySQL (MariaDB renders it as longtext).
    assert columns["payload"]["classification"] == "json"
    # Low-cardinality ENUM classifies categorical.
    assert columns["status"]["classification"] == "categorical"

    ddl = (print_dir / db / "herbarium_sheet/ddl.sql").read_text()
    assert "AUTO_INCREMENT=" not in ddl  # volatile counter stripped per SPEC 2.1.3
    assert "AUTO_INCREMENT" in ddl  # column keyword preserved
