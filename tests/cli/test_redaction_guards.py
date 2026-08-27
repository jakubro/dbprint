"""A refused config or a predicate over withheld values is refused by every CLI entry point."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from dbprint.adapters import (
    ColumnMeta,
    ColumnStats,
    CommentsMeta,
    Frequencies,
    MockAdapter,
    MockTable,
    Range,
    ValueCount,
)
from dbprint.cli.main import main


EXIT_OK = 0
EXIT_GENERIC = 1
EXIT_DRIFT = 3

HASH_RULE = """\
    redact:
      - columns: ["*.email"]
        with: hash
"""

MASK_RULE = """\
    redact:
      - columns: ["*.received_at"]
        with: mask
"""

BOUNDS_ASSERTIONS = """\
    assertions:
      tables:
        seedbank.accession:
          columns:
            received_at:
              range.min: {min: "2000-01-01"}
              percentiles.p99: {max: "2030-01-01"}
"""

ACCEPTED_VALUES_ASSERTIONS = """\
    assertions:
      tables:
        seedbank.collector:
          columns:
            email:
              accepted_values: ["a@example.com"]
"""

CONTROL_ASSERTIONS = """\
    assertions:
      tables:
        seedbank.accession:
          columns:
            provenance_country:
              accepted_values: ["AU", "CA", "DE", "FR", "GB", "IE", "NL", "NZ", "US", "ZA"]
              cardinality: 10
"""


def _project(*blocks: str) -> str:
    return (
        "connections:\n"
        "  primary:\n"
        "    adapter: postgres\n"
        "    auto: true\n"
        "    output: prints\n" + "".join(blocks)
    )


def _pair_column(
    sql_type: str,
    values: tuple[Any, Any],
    *,
    nullable: bool = False,
) -> ColumnStats:
    """A small two-value filler column: only its name and sql_type matter."""

    return ColumnStats(
        sql_type=sql_type,
        nullable=nullable,
        null_count=0,
        null_rate=0.0,
        cardinality=2,
        cardinality_ratio=0.01,
        cardinality_method="exact",
        values=(ValueCount(value=values[0], count=120), ValueCount(value=values[1], count=80)),
        values_coverage=1.0,
        distribution="uniform",
    )


def _categorical_column(sql_type: str, values: tuple[Any, ...]) -> ColumnStats:
    """A closed, exhaustively-listed domain evenly split over 200 rows."""

    count = 200 // len(values)

    return ColumnStats(
        sql_type=sql_type,
        nullable=False,
        null_count=0,
        null_rate=0.0,
        cardinality=len(values),
        cardinality_ratio=round(len(values) / 200, 6),
        cardinality_method="exact",
        values=tuple(ValueCount(value=v, count=count) for v in values),
        values_coverage=1.0,
        distribution="uniform",
    )


def _temporal_column(sql_type: str) -> ColumnStats:
    """Cardinality above the enumeration threshold, so classification stays temporal (SPEC 4.2)."""

    return ColumnStats(
        sql_type=sql_type,
        nullable=False,
        null_count=0,
        null_rate=0.0,
        cardinality=120,
        cardinality_ratio=0.6,
        cardinality_method="exact",
        distribution="uniform",
        frequencies=Frequencies(top=2, bottom=1, listed=120, total=200),
        range=Range(min="2024-01-01", max="2026-06-08", span_days=889),
        percentiles={"p01": "2024-01-01", "p50": "2025-03-04", "p99": "2026-06-08"},
    )


def _json_column(sql_type: str) -> ColumnStats:
    """A `json` classification: a cardinality, no values list, no distribution (SPEC 3)."""

    return ColumnStats(
        sql_type=sql_type,
        nullable=True,
        null_count=0,
        null_rate=0.0,
        cardinality=2,
        cardinality_ratio=0.01,
        cardinality_method="exact",
    )


def _email_column() -> ColumnStats:
    """`email` - the mask/hash target. Skewed and non-unique, same shape proven valid before."""

    emails = ("a@example.com", "b@example.com", "c@example.com")

    return ColumnStats(
        sql_type="character varying(320)",
        nullable=False,
        null_count=0,
        null_rate=0.0,
        cardinality=3,
        cardinality_ratio=0.015,
        cardinality_method="exact",
        values=(
            ValueCount(value=emails[0], count=100),
            ValueCount(value=emails[1], count=60),
            ValueCount(value=emails[2], count=40),
        ),
        values_coverage=1.0,
        distribution="imbalanced",
    )


def _collector_fixture() -> dict[str, MockTable]:
    """`seedbank.collector` - the print's real 10-column table; `email` is already masked there.

    Only `email` is asserted on; the other nine are filler in the table's real shape.
    """

    return {
        "seedbank.collector": MockTable(
            type="table",
            namespace_path=("seedbank", "collector"),
            ddl=(
                "CREATE TABLE seedbank.collector (\n"
                "    collector_id uuid NOT NULL,\n"
                "    full_name character varying(120) NOT NULL,\n"
                "    email character varying(320) NOT NULL,\n"
                "    phone character varying(24) NOT NULL,\n"
                "    institution character varying(120) NOT NULL,\n"
                "    institution_email character varying(320) NOT NULL,\n"
                "    street_address character varying(200) NOT NULL,\n"
                "    postal_code character varying(12) NOT NULL,\n"
                "    country_code character(2) NOT NULL,\n"
                "    hired_on date NOT NULL\n"
                ");\n\n"
                "ALTER TABLE ONLY seedbank.collector\n"
                "    ADD CONSTRAINT collector_pkey PRIMARY KEY (collector_id);\n"
            ),
            columns=[
                ColumnMeta(name=name, sql_type=sql_type, nullable=False, default=None, ordinal=i)
                for i, (name, sql_type) in enumerate(
                    [
                        ("collector_id", "uuid"),
                        ("full_name", "character varying(120)"),
                        ("email", "character varying(320)"),
                        ("phone", "character varying(24)"),
                        ("institution", "character varying(120)"),
                        ("institution_email", "character varying(320)"),
                        ("street_address", "character varying(200)"),
                        ("postal_code", "character varying(12)"),
                        ("country_code", "character(2)"),
                        ("hired_on", "date"),
                    ],
                    start=1,
                )
            ],
            relationships=[],
            indexes=[],
            comments=CommentsMeta(table=None, columns={}),
            stats={
                "collector_id": _pair_column(
                    "uuid",
                    (
                        "00000000-0000-7000-8000-000000000001",
                        "00000000-0000-7000-8000-000000000002",
                    ),
                ),
                "full_name": _pair_column("character varying(120)", ("Ada Alvarez", "Bodhi Chen")),
                "email": _email_column(),
                "phone": _pair_column("character varying(24)", ("+1-555-0100", "+1-555-0101")),
                "institution": _pair_column(
                    "character varying(120)",
                    ("Example Institute", "Sample Herbarium"),
                ),
                "institution_email": _pair_column(
                    "character varying(320)",
                    ("info@example.org", "contact@example.org"),
                ),
                "street_address": _pair_column(
                    "character varying(200)",
                    ("1 Example Way", "2 Sample Road"),
                ),
                "postal_code": _pair_column("character varying(12)", ("SW1A 1AA", "SW1A 2BB")),
                "country_code": _pair_column("character(2)", ("US", "CA")),
                "hired_on": _temporal_column("date"),
            },
            samples={"email": ["a@example.com", "b@example.com", "c@example.com"]},
            row_count=200,
        ),
    }


def _accession_fixture() -> dict[str, MockTable]:
    """`seedbank.accession` - the print's real 16-column table.

    `received_at` is the masked target and `provenance_country` the unredacted control; the
    other fourteen are filler in the table's real shape.
    """

    columns = [
        ("accession_id", "bigint"),
        ("accession_code", "character varying(24)"),
        ("taxon_id", "integer"),
        ("collector_id", "uuid"),
        ("vault_id", "integer"),
        ("shelf_code", "character varying(8)"),
        ("sheet_number", "character varying(12)"),
        ("provenance_country", "character(2)"),
        ("catalogue_url", "character varying(200)"),
        ("traits", "jsonb"),
        ("field_notes", "text"),
        ("viability_pct", "numeric(5,2)"),
        ("seed_count", "integer"),
        ("collected_on", "date"),
        ("received_at", "timestamp(0) with time zone"),
        ("storage_temperature_c", "numeric(4,1)"),
    ]

    stats: dict[str, ColumnStats] = {
        "accession_id": _pair_column("bigint", (1, 2)),
        "accession_code": _pair_column(
            "character varying(24)",
            ("ACC-000001", "ACC-000002"),
        ),
        "taxon_id": _pair_column("integer", (1, 2)),
        "collector_id": _pair_column(
            "uuid",
            (
                "00000000-0000-7000-8000-000000000001",
                "00000000-0000-7000-8000-000000000002",
            ),
        ),
        "vault_id": _pair_column("integer", (1, 2)),
        "shelf_code": _pair_column("character varying(8)", ("A", "B")),
        "sheet_number": _pair_column("character varying(12)", ("001", "002")),
        "provenance_country": _categorical_column(
            "character(2)",
            ("AU", "CA", "DE", "FR", "GB", "IE", "NL", "NZ", "US", "ZA"),
        ),
        "catalogue_url": _pair_column(
            "character varying(200)",
            (
                "https://specimens.example.org/accession/1",
                "https://specimens.example.org/accession/2",
            ),
        ),
        "traits": _json_column("jsonb"),
        "field_notes": _pair_column("text", ("Collected near the ridge.", "Collected by river.")),
        "viability_pct": _pair_column("numeric(5,2)", (20.5, 45.0)),
        "seed_count": _pair_column("integer", (100, 200)),
        "collected_on": _temporal_column("date"),
        "received_at": _temporal_column("timestamp(0) with time zone"),
        "storage_temperature_c": _pair_column("numeric(4,1)", (-19.5, -20.0), nullable=True),
    }

    return {
        "seedbank.accession": MockTable(
            type="table",
            namespace_path=("seedbank", "accession"),
            ddl=(
                "CREATE TABLE seedbank.accession (\n"
                + ",\n".join(
                    f"    {name} {sql_type}"
                    + ("" if name in ("traits", "storage_temperature_c") else " NOT NULL")
                    for name, sql_type in columns
                )
                + "\n);\n\n"
                "ALTER TABLE ONLY seedbank.accession\n"
                "    ADD CONSTRAINT accession_pkey PRIMARY KEY (accession_id);\n"
            ),
            columns=[
                ColumnMeta(
                    name=name,
                    sql_type=sql_type,
                    nullable=name in ("traits", "storage_temperature_c"),
                    default=None,
                    ordinal=i,
                )
                for i, (name, sql_type) in enumerate(columns, start=1)
            ],
            relationships=[],
            indexes=[],
            comments=CommentsMeta(table=None, columns={}),
            stats=stats,
            samples={"provenance_country": ["AU", "CA", "DE", "FR", "GB"]},
            row_count=200,
        ),
    }


class _CollectorAdapter(MockAdapter):
    REQUIRED_KEYS = ("host", "port", "database", "user", "password")

    def __init__(self, _credentials: dict[str, str]) -> None:
        super().__init__(_collector_fixture())


class _AccessionAdapter(MockAdapter):
    REQUIRED_KEYS = ("host", "port", "database", "user", "password")

    def __init__(self, _credentials: dict[str, str]) -> None:
        super().__init__(_accession_fixture())


def _patch_registry(adapter: type[MockAdapter] = _CollectorAdapter):
    return patch.dict(
        "dbprint.cli.adapter_registry.ADAPTERS",
        {"postgres": adapter},
        clear=True,
    )


def _credentials(monkeypatch: pytest.MonkeyPatch, *, salt: str | None = None) -> None:
    for key, value in {
        "DBPRINT_PRIMARY_HOST": "h",
        "DBPRINT_PRIMARY_PORT": "5432",
        "DBPRINT_PRIMARY_DATABASE": "d",
        "DBPRINT_PRIMARY_USER": "u",
        "DBPRINT_PRIMARY_PASSWORD": "p",
    }.items():
        monkeypatch.setenv(key, value)

    monkeypatch.delenv("DBPRINT_PRIMARY_REDACTION_SALT", raising=False)

    if salt is not None:
        monkeypatch.setenv("DBPRINT_PRIMARY_REDACTION_SALT", salt)


def _run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *args: str,
    config: str,
    adapter: type[MockAdapter] = _CollectorAdapter,
):
    (tmp_path / ".dbprint.yaml").write_text(config)
    monkeypatch.chdir(tmp_path)

    with _patch_registry(adapter):
        return CliRunner().invoke(main, list(args))


def _seed_baseline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    config: str,
    *,
    adapter: type[MockAdapter] = _CollectorAdapter,
) -> None:
    """Write a print so `diff` and `check` have a baseline to work against."""

    result = _run(tmp_path, monkeypatch, "generate", config=config, adapter=adapter)

    assert result.exit_code in (EXIT_OK, EXIT_DRIFT), result.output


def _issue_codes(payload: str) -> set[str]:
    parsed = json.loads(payload)

    return {
        issue["code"]
        for connection in parsed
        for issue in connection["assertion_issues"] + connection["drift_issues"]
    }


class TestTheSaltRefusalHoldsOnEveryCommand:
    """`with: hash` and no salt is a config error, and every entry point says so.

    SPEC 2.2.9 forbids defaulting the salt; hashing without one produces an empty digest.
    """

    @pytest.mark.parametrize(
        "command",
        [["generate", "--force"], ["diff"], ["check", "--online"]],
    )
    def test_a_saltless_hash_rule_is_refused(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        command: list[str],
    ) -> None:
        _credentials(monkeypatch, salt="seed")
        _seed_baseline(tmp_path, monkeypatch, _project(HASH_RULE))
        _credentials(monkeypatch)
        result = _run(tmp_path, monkeypatch, *command, config=_project(HASH_RULE))

        assert result.exit_code == EXIT_GENERIC, result.output
        assert "redaction_salt" in result.output

    @pytest.mark.parametrize(
        "command",
        [["generate", "--force"], ["diff"], ["check", "--online"]],
    )
    def test_a_configured_salt_lets_every_command_run(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        command: list[str],
    ) -> None:
        _credentials(monkeypatch, salt="seed")
        _seed_baseline(tmp_path, monkeypatch, _project(HASH_RULE))
        result = _run(tmp_path, monkeypatch, *command, config=_project(HASH_RULE))

        assert result.exit_code in (EXIT_OK, EXIT_DRIFT), result.output

    @pytest.mark.parametrize(
        "command",
        [["generate", "--force"], ["diff"], ["check", "--online"]],
    )
    def test_a_mask_rule_needs_no_salt_on_any_command(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        command: list[str],
    ) -> None:
        """The refusal is specific to `hash`; the other primitives substitute no digest."""

        _credentials(monkeypatch)
        _seed_baseline(tmp_path, monkeypatch, _project(MASK_RULE), adapter=_AccessionAdapter)
        result = _run(
            tmp_path,
            monkeypatch,
            *command,
            config=_project(MASK_RULE),
            adapter=_AccessionAdapter,
        )

        assert result.exit_code in (EXIT_OK, EXIT_DRIFT), result.output

    def test_the_hashed_digest_uses_the_configured_salt(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Two salts, two artifacts - the guard is not satisfied by an empty default."""

        digests = []

        for salt in ("one", "two"):
            root = tmp_path / salt
            root.mkdir()
            _credentials(monkeypatch, salt=salt)
            _seed_baseline(root, monkeypatch, _project(HASH_RULE))
            digests.append(
                (
                    root / "prints" / "primary" / "seedbank" / "collector" / "statistics.yaml"
                ).read_text(),
            )

        assert digests[0] != digests[1]


class TestARedactedColumnRefusesEveryValueBearingPredicate:
    """The guard checks one stat name; `range.min`/`max` and `percentiles.<key>` refuse too."""

    @pytest.mark.parametrize("command", [["check"], ["check", "--online"]])
    def test_bounds_on_a_masked_column_are_refused(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        command: list[str],
    ) -> None:
        config = _project(MASK_RULE, BOUNDS_ASSERTIONS)
        _credentials(monkeypatch)
        _seed_baseline(tmp_path, monkeypatch, config, adapter=_AccessionAdapter)
        result = _run(
            tmp_path,
            monkeypatch,
            *command,
            "--format",
            "json",
            config=config,
            adapter=_AccessionAdapter,
        )
        codes = _issue_codes(result.stdout)

        assert "assertion.redacted-stat" in codes
        assert "assertion.malformed-predicate" not in codes
        assert "assertion.range-out-of-bounds" not in codes
        assert "assertion.percentile-mismatch" not in codes

    @pytest.mark.parametrize("command", [["check"], ["check", "--online"]])
    def test_every_value_bearing_predicate_gets_its_own_refusal(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        command: list[str],
    ) -> None:
        config = _project(MASK_RULE, BOUNDS_ASSERTIONS)
        _credentials(monkeypatch)
        _seed_baseline(tmp_path, monkeypatch, config, adapter=_AccessionAdapter)
        result = _run(
            tmp_path,
            monkeypatch,
            *command,
            "--format",
            "json",
            config=config,
            adapter=_AccessionAdapter,
        )
        refusals = [
            issue
            for connection in json.loads(result.stdout)
            for issue in connection["assertion_issues"]
            if issue["code"] == "assertion.redacted-stat"
        ]

        column_path = "assertions.primary.tables.seedbank.accession.columns.received_at"

        assert {i["path"] for i in refusals} == {
            f"{column_path}.range.min",
            f"{column_path}.percentiles.p99",
        }

    @pytest.mark.parametrize("command", [["check"], ["check", "--online"]])
    def test_accepted_values_on_a_redacted_column_is_refused(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        command: list[str],
    ) -> None:
        """The guard's one already-covered stat is refused on both check modes."""

        config = _project(MASK_RULE.replace("received_at", "email"), ACCEPTED_VALUES_ASSERTIONS)
        _credentials(monkeypatch)
        _seed_baseline(tmp_path, monkeypatch, config)
        result = _run(tmp_path, monkeypatch, *command, "--format", "json", config=config)
        codes = _issue_codes(result.stdout)

        assert "assertion.redacted-stat" in codes
        assert "assertion.accepted-values-violated" not in codes

    @pytest.mark.parametrize("command", [["check"], ["check", "--online"]])
    def test_the_refusal_stays_a_warning(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        command: list[str],
    ) -> None:
        """A fix making redaction more honest must not start failing pipelines."""

        config = _project(MASK_RULE, BOUNDS_ASSERTIONS)
        _credentials(monkeypatch)
        _seed_baseline(tmp_path, monkeypatch, config, adapter=_AccessionAdapter)
        result = _run(tmp_path, monkeypatch, *command, config=config, adapter=_AccessionAdapter)

        assert result.exit_code == EXIT_OK, result.output

    @pytest.mark.parametrize("command", [["check"], ["check", "--online"]])
    def test_a_bare_parent_name_keeps_the_warning_it_has_always_had(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        command: list[str],
    ) -> None:
        """`range:` is not assertable, but on a redacted column it was never an error."""

        bare = """\
    assertions:
      tables:
        seedbank.accession:
          columns:
            received_at:
              range: {min: "2000-01-01"}
"""
        config = _project(MASK_RULE, bare)
        _credentials(monkeypatch)
        _seed_baseline(tmp_path, monkeypatch, config, adapter=_AccessionAdapter)
        result = _run(
            tmp_path,
            monkeypatch,
            *command,
            "--format",
            "json",
            config=config,
            adapter=_AccessionAdapter,
        )
        codes = _issue_codes(result.stdout)

        assert result.exit_code == EXIT_OK, result.output
        assert "assertion.redacted-stat" in codes
        assert "assertion.unknown-stat" not in codes

    @pytest.mark.parametrize("stat", ["freshness.classification", "looks_like", "candidate_key"])
    def test_a_measurement_on_a_redacted_column_is_still_evaluated(self, stat: str) -> None:
        """SPEC 2.2.9 leaves these untouched, so the refusal must not swallow them."""

        from dbprint.assertions.predicate import is_value_bearing_stat

        assert not is_value_bearing_stat(stat)

    @pytest.mark.parametrize("command", [["check"], ["check", "--online"]])
    def test_an_unredacted_column_is_evaluated_normally(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        command: list[str],
    ) -> None:
        config = _project(MASK_RULE, CONTROL_ASSERTIONS)
        _credentials(monkeypatch)
        _seed_baseline(tmp_path, monkeypatch, config, adapter=_AccessionAdapter)
        result = _run(
            tmp_path,
            monkeypatch,
            *command,
            "--format",
            "json",
            config=config,
            adapter=_AccessionAdapter,
        )

        assert result.exit_code == EXIT_OK, result.output
        assert "assertion.redacted-stat" not in _issue_codes(result.stdout)

    def test_the_two_modes_answer_from_the_same_evidence(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """One config, one database, one verdict - the property both guards must have."""

        config = _project(MASK_RULE, BOUNDS_ASSERTIONS)
        _credentials(monkeypatch)
        _seed_baseline(tmp_path, monkeypatch, config, adapter=_AccessionAdapter)
        offline = _run(
            tmp_path,
            monkeypatch,
            "check",
            "--format",
            "json",
            config=config,
            adapter=_AccessionAdapter,
        )
        online = _run(
            tmp_path,
            monkeypatch,
            "check",
            "--online",
            "--format",
            "json",
            config=config,
            adapter=_AccessionAdapter,
        )

        assert _issue_codes(offline.stdout) == _issue_codes(online.stdout)
        assert offline.exit_code == online.exit_code


class TestTheLivePayloadCarriesWhatItDeclares:
    """A payload marked redacted must not hold the literals it says it withheld."""

    def test_the_live_statistics_carry_the_marker_and_the_substitution(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from dbprint.config import load_project
        from dbprint.engine import DiffRequest, Engine

        config = _project(MASK_RULE.replace("received_at", "email"))
        _credentials(monkeypatch)
        _seed_baseline(tmp_path, monkeypatch, config)
        conn = load_project(tmp_path).connections["primary"]
        result = Engine(_CollectorAdapter({}), conn, tmp_path).compute_diff(DiffRequest())
        column: dict[str, Any] = result.live_statistics["seedbank.collector"]["columns"]["email"]

        assert column["redacted"] == "mask"
        assert {e["value"] for e in column["values"]} == {"[redacted]"}


class TestTheRefusalIsNameable:
    """A config the layer refuses is named in both output forms, not just on stderr."""

    @staticmethod
    def _refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *args: str):
        _credentials(monkeypatch, salt="seed")
        _seed_baseline(tmp_path, monkeypatch, _project(HASH_RULE))
        _credentials(monkeypatch)

        return _run(tmp_path, monkeypatch, *args, config=_project(HASH_RULE))

    def test_the_machine_envelope_names_the_cause(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        result = self._refused(tmp_path, monkeypatch, "check", "--online", "--format", "json")
        payload = json.loads(result.stdout)[0]

        assert payload["exit_code"] == EXIT_GENERIC
        assert [n["subject"] for n in payload["not_run"]] == ["primary"]
        assert "redaction_salt" in payload["not_run"][0]["cause"]

    def test_the_summary_counts_it(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        result = self._refused(tmp_path, monkeypatch, "check", "--online", "--format", "json")

        assert json.loads(result.stdout)[0]["summary"]["not_run_count"] == 1

    def test_the_human_output_carries_it_under_its_own_heading(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        result = self._refused(tmp_path, monkeypatch, "check", "--online")

        assert "did not run" in result.stdout
        assert "redaction_salt" in result.stdout

    def test_the_human_output_never_calls_it_drift(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """No comparison happened, so the heading that names one would be a lie."""

        result = self._refused(tmp_path, monkeypatch, "check", "--online")

        assert "schema drift" not in result.stdout

    def test_the_exit_code_is_unmoved(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        result = self._refused(tmp_path, monkeypatch, "check", "--online")

        assert result.exit_code == EXIT_GENERIC
