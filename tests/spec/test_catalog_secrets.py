"""SPEC 2.1.3's credential masks over catalog text: what each rule masks, and what none touches."""

from __future__ import annotations

import pytest

from dbprint.spec.catalog_secrets import MASK, mask_ddl, mask_text, names_secret
from tests._secrets import FORMATS, jwt, pem


_HEAD = "CREATE TABLE `t` (`id` int)"
_PW = "Zq9vWm2rTx"


class TestARemoteTablesCredential:
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
    def test_each_documented_credential_place_is_masked(self, options: str, expected: str) -> None:
        assert mask_ddl(f"{_HEAD} {options}") == f"{_HEAD} {expected}"

    @pytest.mark.parametrize(
        "options",
        [
            "ENGINE=FEDERATED CONNECTION='mysql://someuser@h:3399/db/t'",
            "ENGINE=FEDERATED CONNECTION='s/tbl'",
        ],
        ids=["no-password", "server-name"],
    )
    def test_a_ddl_carrying_no_password_is_unchanged(self, options: str) -> None:
        assert mask_ddl(f"{_HEAD} {options}") == f"{_HEAD} {options}"

    def test_a_local_engine_is_masked_too_and_a_user_at_host_is_no_url(self) -> None:
        options = "ENGINE=InnoDB COMMENT='user:pw@x' CONNECTION='mysql://u:pw@h/db/t'"

        assert mask_ddl(f"{_HEAD} {options}") == (
            f"{_HEAD} ENGINE=InnoDB COMMENT='user:pw@x' CONNECTION='mysql://u:[HIDDEN]@h/db/t'"
        )

    def test_a_column_comment_is_read_like_the_tables(self) -> None:
        column = "CREATE TABLE `t` (`id` int COMMENT 'password \"p\"')"

        assert mask_ddl(f"{column} ENGINE=SPIDER COMMENT='password \"q\"'") == (
            "CREATE TABLE `t` (`id` int COMMENT 'password \"[HIDDEN]\"') "
            "ENGINE=SPIDER COMMENT='password \"[HIDDEN]\"'"
        )

    def test_a_url_user_part_is_masked_wherever_it_sits(self) -> None:
        assert mask_ddl("URL('https://u:pw@h/x.csv', 'CSV')") == (
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
    def test_a_password_with_quotes_or_at_signs_is_masked_whole(
        self,
        options: str,
        expected: str,
    ) -> None:
        assert mask_ddl(f"{_HEAD} {options}") == f"{_HEAD} {expected}"


class TestClickhouseText:
    def test_a_url_password_in_a_default_is_masked_as_in_the_engine(self) -> None:
        raw = (
            "CREATE TABLE seedbank.narrow (`id` UInt64 DEFAULT 'https://a:b@c') "
            "ENGINE = URL('https://u:pw@h/x.csv', 'CSV')"
        )

        assert mask_ddl(raw) == (
            "CREATE TABLE seedbank.narrow (`id` UInt64 DEFAULT 'https://a:[HIDDEN]@c') "
            "ENGINE = URL('https://u:[HIDDEN]@h/x.csv', 'CSV')"
        )

    def test_a_comment_is_masked_beside_the_engine_arguments(self) -> None:
        raw = (
            "CREATE TABLE seedbank.narrow (`id` UInt64) "
            "ENGINE = URL('https://u:pw@h/x(1).csv', 'CSV') "
            "COMMENT 'see https://a:b@c and Password=keep'"
        )

        assert mask_ddl(raw) == (
            "CREATE TABLE seedbank.narrow (`id` UInt64) "
            "ENGINE = URL('https://u:[HIDDEN]@h/x(1).csv', 'CSV') "
            "COMMENT 'see https://a:[HIDDEN]@c and Password=[HIDDEN]'"
        )

    def test_an_escaped_quote_or_at_sign_in_a_url_password_is_masked_whole(self) -> None:
        raw = (
            "CREATE TABLE seedbank.narrow (`id` UInt64) "
            r"ENGINE = URL('https://u:a\'b@c@h/x.csv', 'CSV')"
        )

        assert mask_ddl(raw) == (
            "CREATE TABLE seedbank.narrow (`id` UInt64) "
            "ENGINE = URL('https://u:[HIDDEN]@h/x.csv', 'CSV')"
        )


_SK = FORMATS["sk_live"]()
_BCRYPT = FORMATS["bcrypt"]()
_PEM = pem()
_JWT = jwt()

_SHAPES = [
    pytest.param(
        f"SELECT 'https://exporter:{_PW}@host.example/x' AS src",
        "SELECT 'https://exporter:[HIDDEN]@host.example/x' AS src",
        id="url-in-a-view",
    ),
    pytest.param(
        f"SELECT * FROM dblink('host=h user=u password={_PW} dbname=d', 'SELECT 1')",
        "SELECT * FROM dblink('host=h user=u password=[HIDDEN] dbname=d', 'SELECT 1')",
        id="connection-string",
    ),
    pytest.param(
        f"SELECT * FROM postgres_scan('dbname=d password={_PW}', 'public', 'garden')",
        "SELECT * FROM postgres_scan('dbname=d password=[HIDDEN]', 'public', 'garden')",
        id="postgres-scan",
    ),
    pytest.param(
        f"note text DEFAULT 'password={_PW}'::text",
        "note text DEFAULT 'password=[HIDDEN]'::text",
        id="default-pair",
    ),
    pytest.param(
        f"CONSTRAINT code_check CHECK ((code <> '{_SK}'::text))",
        "CONSTRAINT code_check CHECK ((code <> '[HIDDEN]'::text))",
        id="check-recognised-key",
    ),
    pytest.param(
        f"COMMENT ON COLUMN public.herbarium.biome IS 'api_key={_PW}';",
        "COMMENT ON COLUMN public.herbarium.biome IS 'api_key=[HIDDEN]';",
        id="comment-on",
    ),
    pytest.param(
        f"`biome` varchar(8) COMMENT 'token: {_PW}'",
        "`biome` varchar(8) COMMENT 'token: [HIDDEN]'",
        id="inline-comment",
    ),
    pytest.param(
        "/*!50100 PARTITION BY RANGE (`id`) "
        f"(PARTITION p0 VALUES LESS THAN (10) COMMENT = 'api_key={_PW}' ENGINE = InnoDB) */",
        "/*!50100 PARTITION BY RANGE (`id`) "
        "(PARTITION p0 VALUES LESS THAN (10) COMMENT = 'api_key=[HIDDEN]' ENGINE = InnoDB) */",
        id="mysql-executable-comment",
    ),
    pytest.param(
        f"SELECT a.id /*! it's */, 'host=h password={_PW}' AS c",
        "SELECT a.id /*! it's */, 'host=h password=[HIDDEN]' AS c",
        id="apostrophe-in-a-bang-comment",
    ),
    pytest.param(
        f"""SELECT '{{"api_key": "{_PW}", "region": "eu"}}' AS doc""",
        """SELECT '{"api_key": "[HIDDEN]", "region": "eu"}' AS doc""",
        id="json",
    ),
    pytest.param(
        f"WHERE g.api_key = '{_PW}'",
        "WHERE g.api_key = '[HIDDEN]'",
        id="compared-to-a-credential-column",
    ),
    pytest.param(
        f"WHERE ((password)::text = '{_PW}'::text)",
        "WHERE ((password)::text = '[HIDDEN]'::text)",
        id="through-a-cast",
    ),
    pytest.param(
        f"WHERE ((code)::character varying = 'kept'::text) AND password = '{_PW}'",
        "WHERE ((code)::character varying = 'kept'::text) AND password = '[HIDDEN]'",
        id="after-a-multi-word-cast",
    ),
    pytest.param(
        f"""WHERE "api_key" = '{_PW}'""",
        """WHERE "api_key" = '[HIDDEN]'""",
        id="quoted-identifier",
    ),
    pytest.param(
        f"WHERE `api_key` = '{_PW}'",
        "WHERE `api_key` = '[HIDDEN]'",
        id="backticked-identifier",
    ),
    pytest.param(
        f"SELECT lookup(api_key => '{_PW}')",
        "SELECT lookup(api_key => '[HIDDEN]')",
        id="named-argument",
    ),
    pytest.param(f"SELECT '{_BCRYPT}' AS hashed", "SELECT '[HIDDEN]' AS hashed", id="bcrypt"),
    pytest.param(f"SELECT '{_PEM}' AS keyfile", "SELECT '[HIDDEN]' AS keyfile", id="pem"),
    pytest.param(
        f"SELECT 'Bearer {_JWT}' AS auth_header",
        "SELECT 'Bearer [HIDDEN]' AS auth_header",
        id="jwt",
    ),
    pytest.param(
        f"OPTIONS (table_name 'garden', password '{_PW}')",
        "OPTIONS (table_name 'garden', password '[HIDDEN]')",
        id="foreign-table-option",
    ),
    pytest.param(
        f"""SELECT 'config="password={_PW}"' AS nested""",
        """SELECT 'config="password=[HIDDEN]"' AS nested""",
        id="pair-inside-a-pair",
    ),
    pytest.param(
        f"SELECT 'redis://:{_PW}@host/0' AS cache",
        "SELECT 'redis://:[HIDDEN]@host/0' AS cache",
        id="url-without-a-user",
    ),
]


class TestEveryShape:
    @pytest.mark.parametrize(("raw", "expected"), _SHAPES)
    def test_the_secret_alone_is_masked(self, raw: str, expected: str) -> None:
        assert mask_ddl(raw) == expected

    @pytest.mark.parametrize(("raw", "expected"), _SHAPES)
    def test_masking_is_idempotent(self, raw: str, expected: str) -> None:
        del raw

        assert mask_ddl(expected) == expected

    def test_a_literal_after_one_ending_in_a_backslash_is_still_read(self) -> None:
        raw = f"SELECT 'C:\\' AS p, 'host=h password={_PW}' AS c"

        assert mask_ddl(raw) == "SELECT 'C:\\' AS p, 'host=h password=[HIDDEN]' AS c"

    def test_a_rotated_secret_leaves_the_text_identical(self) -> None:
        def view(secret: str) -> str:
            return f"SELECT 'https://exporter:{secret}@host.example/x' AS src"

        assert mask_ddl(view(_PW)) == mask_ddl(view(_PW[::-1]))


class TestNothingElseChanges:
    @pytest.mark.parametrize(
        "raw",
        [
            pytest.param("CREATE TABLE t (s STRUCT<password: STRING>)", id="struct-type"),
            pytest.param("ON a.api_key = b.api_key", id="join"),
            pytest.param("ON a.api_key=b.api_key", id="unspaced-join"),
            pytest.param("biome VARCHAR DEFAULT 'pending'", id="ordinary-default"),
            pytest.param("""doc VARCHAR DEFAULT '{"token": null}'""", id="json-null"),
            pytest.param("token TEXT DEFAULT 'abc'", id="default-on-a-credential-name"),
            pytest.param("api_key VARCHAR", id="credential-named-column"),
            pytest.param("SELECT 'https://host.example/open' AS src", id="url-without-password"),
            pytest.param("CREATE DEFINER=`root`@`localhost` VIEW v AS SELECT 1", id="definer"),
            pytest.param("SELECT date_trunc('month', ts)", id="function-argument"),
            pytest.param("SELECT TIMESTAMP '2020-01-01'", id="typed-literal"),
            pytest.param("SELECT E'a\\nb'", id="escape-string"),
            pytest.param("x TEXT DEFAULT 'password='", id="empty-value"),
            pytest.param("x TEXT DEFAULT 'secret: true'", id="boolean-value"),
            pytest.param(f"SELECT '{jwt({'kid': 'seedbank'})}' AS t", id="dotted-without-alg"),
            pytest.param(
                "SELECT a.id -- don't\nFROM garden a JOIN garden b ON a.api_key=b.api_key",
                id="apostrophe-in-a-line-comment",
            ),
            pytest.param(
                "SELECT a.id /* it's */ FROM garden a JOIN garden b ON a.api_key=b.api_key",
                id="apostrophe-in-a-block-comment",
            ),
        ],
    )
    def test_the_text_is_kept_verbatim(self, raw: str) -> None:
        assert mask_ddl(raw) == raw


class TestPlainText:
    def test_a_spider_comment_is_masked(self) -> None:
        assert mask_text('wrapper "mysql", user "u", password "p"') == (
            'wrapper "mysql", user "u", password "[HIDDEN]"'
        )

    def test_a_user_at_host_is_no_url(self) -> None:
        assert mask_text("user:pw@x") == "user:pw@x"

    def test_prose_after_a_secret_key_pays_the_recall_cost(self) -> None:
        assert mask_text("the token: rotated weekly") == f"the token: {MASK} weekly"


@pytest.mark.parametrize(
    ("key", "secret"),
    [
        ("PWD", True),
        ("pw", True),
        ("x-api-key", True),
        ("REMOTE_PASSWORD", True),
        ("access_key", True),
        ("webhook_secret", True),
        ("aws_access_key_id", False),
        ("user", False),
        ("region", False),
    ],
)
def test_which_keys_name_a_secret(key: str, secret: bool) -> None:
    assert names_secret(key) is secret
