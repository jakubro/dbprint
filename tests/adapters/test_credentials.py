"""A remote table's DDL publishes where its rows live, never the password that reaches them."""

from __future__ import annotations

import pytest

from dbprint.adapters.credentials import mask_mysql_ddl, mask_secrets


_HEAD = "CREATE TABLE `t` (`id` int)"


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        (
            "ENGINE=FEDERATED CONNECTION='mysql://someuser:s3cretpw@127.0.0.1:3399/db/tbl'",
            "ENGINE=FEDERATED CONNECTION='mysql://someuser:[HIDDEN]@127.0.0.1:3399/db/tbl'",
        ),
        (
            "ENGINE=CONNECT CONNECTION='DSN=x;UID=u;PWD=p' `TABLE_TYPE`='ODBC'",
            "ENGINE=CONNECT CONNECTION='DSN=x;UID=u;PWD=[HIDDEN]' `TABLE_TYPE`='ODBC'",
        ),
        (
            "ENGINE=CONNECT OPTION_LIST='User=me,Password=mypass'",
            "ENGINE=CONNECT OPTION_LIST='User=me,Password=[HIDDEN]'",
        ),
        (
            'ENGINE=SPIDER COMMENT=\'wrapper "mysql", user "u", password "p"\'',
            'ENGINE=SPIDER COMMENT=\'wrapper "mysql", user "u", password "[HIDDEN]"\'',
        ),
        (
            "ENGINE=spider REMOTE_PASSWORD='p' REMOTE_USER='u'",
            "ENGINE=spider REMOTE_PASSWORD='[HIDDEN]' REMOTE_USER='u'",
        ),
        (
            "ENGINE=SPIDER `REMOTE_PASSWORD`='p'",
            "ENGINE=SPIDER `REMOTE_PASSWORD`='[HIDDEN]'",
        ),
    ],
    ids=[
        "federated-url",
        "connect-odbc",
        "connect-option-list",
        "spider-comment",
        "spider-option",
        "spider-backticked",
    ],
)
def test_each_documented_credential_place_is_masked(options: str, expected: str) -> None:
    assert mask_mysql_ddl(f"{_HEAD} {options}") == f"{_HEAD} {expected}"


@pytest.mark.parametrize(
    "options",
    [
        "ENGINE=FEDERATED CONNECTION='mysql://someuser@h:3399/db/t'",
        "ENGINE=FEDERATED CONNECTION='s/tbl'",
        "ENGINE=InnoDB COMMENT='user:pw@x' CONNECTION='mysql://u:pw@h/db/t'",
    ],
    ids=["no-password", "server-name", "local-engine"],
)
def test_a_ddl_carrying_no_remote_password_is_unchanged(options: str) -> None:
    assert mask_mysql_ddl(f"{_HEAD} {options}") == f"{_HEAD} {options}"


def test_a_column_comment_is_never_read() -> None:
    column = "CREATE TABLE `t` (`id` int COMMENT 'password \"p\"')"

    assert mask_mysql_ddl(f"{column} ENGINE=SPIDER COMMENT='password \"q\"'") == (
        f"{column} ENGINE=SPIDER COMMENT='password \"[HIDDEN]\"'"
    )


def test_masking_is_idempotent() -> None:
    ddl = f"{_HEAD} ENGINE=FEDERATED CONNECTION='mysql://u:pw@h/db/t'"

    assert mask_mysql_ddl(mask_mysql_ddl(ddl)) == mask_mysql_ddl(ddl)


def test_a_url_user_part_is_masked_wherever_it_sits() -> None:
    assert mask_secrets("URL('https://u:pw@h/x.csv', 'CSV')") == (
        "URL('https://u:[HIDDEN]@h/x.csv', 'CSV')"
    )


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        (
            "ENGINE=FEDERATED CONNECTION='mysql://u:it''s@127.0.0.1:3399/db/t'",
            "ENGINE=FEDERATED CONNECTION='mysql://u:[HIDDEN]@127.0.0.1:3399/db/t'",
        ),
        (
            "ENGINE=FEDERATED CONNECTION='mysql://u:p@ss@127.0.0.1:3399/db/t'",
            "ENGINE=FEDERATED CONNECTION='mysql://u:[HIDDEN]@127.0.0.1:3399/db/t'",
        ),
        (
            r"ENGINE=FEDERATED CONNECTION='mysql://u:back\'s@h/db/t'",
            "ENGINE=FEDERATED CONNECTION='mysql://u:[HIDDEN]@h/db/t'",
        ),
        (
            "ENGINE=SPIDER COMMENT='wrapper \"mysql\", password ''p w'''",
            "ENGINE=SPIDER COMMENT='wrapper \"mysql\", password ''[HIDDEN]'''",
        ),
        (
            "ENGINE=CONNECT CONNECTION='DSN=x;PWD={a;b}'",
            "ENGINE=CONNECT CONNECTION='DSN=x;PWD=[HIDDEN]'",
        ),
    ],
    ids=["doubled-quote", "at-sign", "backslash-quote", "spider-single-quoted", "odbc-braced"],
)
def test_a_password_with_quotes_or_at_signs_is_masked_whole(options: str, expected: str) -> None:
    assert mask_mysql_ddl(f"{_HEAD} {options}") == f"{_HEAD} {expected}"
