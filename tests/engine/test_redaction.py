"""A redacted print differs only in cell values, so most tests assert against a plain baseline."""

from __future__ import annotations

from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from dbprint.adapters import (
    ColumnMeta,
    ColumnStats,
    CommentsMeta,
    Frequencies,
    Length,
    MockAdapter,
    MockTable,
    Range,
    ValueCount,
)
from dbprint.config import ConfigError, load_project
from dbprint.config.project import RedactRule, bind_redaction_salt
from dbprint.engine import Engine
from dbprint.spec.redaction import MASK_PLACEHOLDER, Primitive, redact_value
from tests.conftest import normalize_instants
from tests.engine.test_orchestrator import _conn_config, _curator_fixture
from tests.engine.test_prose_suppression import _fixture as _prose_fixture


def _run(tmp_path: Path, *rules: RedactRule, salt: str | None = None) -> dict[str, Any]:
    return yaml.safe_load(_generate(tmp_path, *rules, salt=salt))


def _generate(tmp_path: Path, *rules: RedactRule, salt: str | None = None) -> str:
    """Profile the curator fixture under `rules` and return the artifact as written."""

    conn = replace(_conn_config(tmp_path), redact=rules, redaction_salt=salt)
    Engine(MockAdapter(_curator_fixture()), conn, tmp_path).generate()

    return (tmp_path / "primary" / "public" / "curator" / "statistics.yaml").read_text()


def _herbarium_id(payload: dict[str, Any]) -> dict[str, Any]:
    return payload["columns"]["herbarium_id"]


def _contacts_fixture() -> dict[str, MockTable]:
    """Twelve addresses under a name `sensitivity` does not recognise, so `contact` can only
    come from the shape; twelve values is few enough for SPEC 2.2.4 to publish every one.
    """

    addresses = sorted(f"user{i}@example.com" for i in range(12))

    return {
        "seedbank.collector": MockTable(
            type="table",
            namespace_path=("seedbank", "collector"),
            ddl="CREATE TABLE seedbank.collector (institution text);\n",
            columns=[
                ColumnMeta(
                    name="institution",
                    sql_type="text",
                    nullable=False,
                    default=None,
                    ordinal=1,
                ),
            ],
            relationships=[],
            indexes=[],
            comments=CommentsMeta(table=None, columns={}),
            stats={
                "institution": ColumnStats(
                    sql_type="text",
                    nullable=False,
                    null_count=0,
                    null_rate=0.0,
                    cardinality=12,
                    cardinality_ratio=0.125,
                    cardinality_method="exact",
                    values=tuple(ValueCount(value=a, count=8) for a in addresses),
                    values_coverage=1.0,
                    distribution="uniform",
                ),
            },
            samples={"institution": addresses},
            row_count=96,
        ),
    }


def _run_contacts(tmp_path: Path, *rules: RedactRule) -> dict[str, Any]:
    conn = replace(_conn_config(tmp_path), redact=rules)
    Engine(MockAdapter(_contacts_fixture()), conn, tmp_path).generate()

    return yaml.safe_load(
        (tmp_path / "primary" / "seedbank" / "collector" / "statistics.yaml").read_text(),
    )


class TestNoRulesChangeNothing:
    def test_no_redact_block_leaves_no_redacted_marker(self, tmp_path: Path) -> None:
        plain = _generate(tmp_path / "a")

        assert "redacted" not in _herbarium_id(yaml.safe_load(plain))

    def test_a_redacted_run_still_differs_from_a_plain_one(self, tmp_path: Path) -> None:
        """The comparison above only means something if a real difference survives it."""

        plain = _generate(tmp_path / "a")
        masked = _generate(tmp_path / "b", RedactRule(columns=("*.herbarium_id",), with_="mask"))

        assert normalize_instants(plain) != normalize_instants(masked)


class TestMask:
    def test_literals_are_replaced(self, tmp_path: Path) -> None:
        col = _herbarium_id(_run(tmp_path, RedactRule(columns=("*.herbarium_id",), with_="mask")))

        assert {e["value"] for e in col["values"]} == {MASK_PLACEHOLDER}

    def test_the_marker_declares_the_primitive(self, tmp_path: Path) -> None:
        col = _herbarium_id(_run(tmp_path, RedactRule(columns=("*.herbarium_id",), with_="mask")))

        assert col["redacted"] == "mask"

    def test_every_measurement_survives(self, tmp_path: Path) -> None:
        """Counts, ratios, coverage and distribution describe data without disclosing it."""

        plain = _herbarium_id(_run(tmp_path / "a"))
        masked = _herbarium_id(
            _run(tmp_path / "b", RedactRule(columns=("*.herbarium_id",), with_="mask")),
        )

        for field in (
            "null_count",
            "null_rate",
            "cardinality",
            "cardinality_ratio",
            "values_coverage",
            "distribution",
            "classification",
        ):
            assert masked[field] == plain[field], field

        assert [e["count"] for e in masked["values"]] == [e["count"] for e in plain["values"]]


class TestDrop:
    def test_entries_keep_counts_and_carry_no_literal(self, tmp_path: Path) -> None:
        col = _herbarium_id(_run(tmp_path, RedactRule(columns=("*.herbarium_id",), with_="drop")))

        assert all("value" not in e for e in col["values"])
        assert [e["count"] for e in col["values"]] == [9, 8]


class TestHash:
    def test_distinct_values_stay_distinct(self, tmp_path: Path) -> None:
        col = _herbarium_id(
            _run(tmp_path, RedactRule(columns=("*.herbarium_id",), with_="hash"), salt="pepper"),
        )
        digests = [e["value"] for e in col["values"]]

        assert len(set(digests)) == len(digests)
        assert MASK_PLACEHOLDER not in digests

    def test_a_stable_salt_gives_a_stable_artifact(self, tmp_path: Path) -> None:
        first = _run(
            tmp_path / "a",
            RedactRule(columns=("*.herbarium_id",), with_="hash"),
            salt="s",
        )
        second = _run(
            tmp_path / "b",
            RedactRule(columns=("*.herbarium_id",), with_="hash"),
            salt="s",
        )

        assert _herbarium_id(first)["values"] == _herbarium_id(second)["values"]

    def test_a_different_salt_gives_different_digests(self, tmp_path: Path) -> None:
        one = _run(tmp_path / "a", RedactRule(columns=("*.herbarium_id",), with_="hash"), salt="s1")
        two = _run(tmp_path / "b", RedactRule(columns=("*.herbarium_id",), with_="hash"), salt="s2")

        assert _herbarium_id(one)["values"] != _herbarium_id(two)["values"]


class TestSaltIsAPrecondition:
    def test_hash_without_a_salt_is_refused(self, tmp_path: Path) -> None:
        """An unsalted digest of an email is reversible, so it is refused not defaulted."""

        conn = replace(
            _conn_config(tmp_path),
            redact=(RedactRule(columns=("*.herbarium_id",), with_="hash"),),
        )

        with pytest.raises(ConfigError, match="redaction_salt"):
            bind_redaction_salt(conn, None)

    @pytest.mark.parametrize("salt", ["", "   "])
    def test_hash_with_a_salt_carrying_no_material_is_refused(
        self,
        tmp_path: Path,
        salt: str,
    ) -> None:
        """A variable exported empty resolves to a string, and hashes exactly as no salt does."""

        conn = replace(
            _conn_config(tmp_path),
            redact=(RedactRule(columns=("*.herbarium_id",), with_="hash"),),
        )

        with pytest.raises(ConfigError, match="redaction_salt"):
            bind_redaction_salt(conn, salt)

    def test_mask_needs_no_salt(self, tmp_path: Path) -> None:
        conn = replace(
            _conn_config(tmp_path),
            redact=(RedactRule(columns=("*.herbarium_id",), with_="mask"),),
        )

        assert bind_redaction_salt(conn, None).redaction_salt is None

    def test_a_blank_salt_reaches_the_config_as_no_salt(self, tmp_path: Path) -> None:
        """One representation downstream, whichever spelling of "nothing" arrived."""

        conn = replace(
            _conn_config(tmp_path),
            redact=(RedactRule(columns=("*.herbarium_id",), with_="mask"),),
        )

        assert bind_redaction_salt(conn, "   ").redaction_salt is None

    def test_a_salt_is_stored_as_configured(self, tmp_path: Path) -> None:
        """Trimming it would change every digest of a secret whose whitespace is its own."""

        conn = replace(
            _conn_config(tmp_path),
            redact=(RedactRule(columns=("*.herbarium_id",), with_="hash"),),
        )

        assert bind_redaction_salt(conn, " pepper ").redaction_salt == " pepper "


class TestTheDigestRefusesASaltWithNoMaterial:
    """The config gate makes this unreachable from the CLI, and the artifact cannot show it."""

    @pytest.mark.parametrize("salt", [None, "", "   "])
    def test_hashing_without_material_raises(self, salt: str | None) -> None:
        with pytest.raises(ValueError, match="redaction_salt"):
            redact_value("alpha", "hash", salt)

    @pytest.mark.parametrize("salt", [None, ""])
    def test_masking_is_unaffected(self, salt: str | None) -> None:
        assert redact_value("alpha", "mask", salt) == MASK_PLACEHOLDER


class TestTargeting:
    def test_a_rule_targets_by_sensitivity(self, tmp_path: Path) -> None:
        col = _herbarium_id(
            _run(tmp_path, RedactRule(sensitivity=("personal_name",), with_="mask")),
        )

        assert "redacted" not in col

    def test_a_rule_targets_by_looks_like(self, tmp_path: Path) -> None:
        payload = _run_contacts(tmp_path, RedactRule(looks_like=("email",), with_="mask"))
        col = payload["columns"]["institution"]

        assert col["redacted"] == "mask"
        assert {e["value"] for e in col["values"]} == {MASK_PLACEHOLDER}

    def test_an_unmatched_column_is_untouched(self, tmp_path: Path) -> None:
        payload = _run(tmp_path, RedactRule(columns=("*.no_such_column",), with_="mask"))

        assert "redacted" not in _herbarium_id(payload)

    def test_the_last_matching_rule_decides(self, tmp_path: Path) -> None:
        payload = _run(
            tmp_path,
            RedactRule(columns=("*.herbarium_id",), with_="mask"),
            RedactRule(columns=("*.herbarium_id",), with_="drop"),
        )

        assert _herbarium_id(payload)["redacted"] == "drop"


class TestASmallColumnIsCoveredByASensitivityRule:
    """A `sensitivity` rule covers only what detection found; for a column named outside the
    contact tokens, detection runs on the value shapes alone.
    """

    def test_the_shape_and_the_category_are_both_reported(self, tmp_path: Path) -> None:
        col = _run_contacts(tmp_path)["columns"]["institution"]

        assert col["inferred"]["looks_like"] == "email"
        assert col["inferred"]["sensitivity"] == "contact"

    def test_a_sensitivity_rule_masks_it(self, tmp_path: Path) -> None:
        payload = _run_contacts(tmp_path, RedactRule(sensitivity=("contact",), with_="mask"))
        col = payload["columns"]["institution"]

        assert col["redacted"] == "mask"
        assert {e["value"] for e in col["values"]} == {MASK_PLACEHOLDER}

    def test_no_address_reaches_disk(self, tmp_path: Path) -> None:
        """The leak is what the file contains, so the file is what gets asserted on."""

        _run_contacts(tmp_path, RedactRule(sensitivity=("contact",), with_="mask"))
        written = (tmp_path / "primary" / "seedbank" / "collector" / "statistics.yaml").read_text()

        assert "@" not in written

    def test_without_a_rule_the_literals_are_still_written(self, tmp_path: Path) -> None:
        """Detection and redaction stay separate - the shape alone withholds nothing."""

        col = _run_contacts(tmp_path)["columns"]["institution"]

        assert "redacted" not in col
        assert all("@" in e["value"] for e in col["values"])


class TestADefaultsRuleReachesTheArtifact:
    """The cascade is only real if the print a project-wide rule governs comes out redacted."""

    def _generate(self, tmp_path: Path, config: str) -> dict[str, Any]:
        (tmp_path / ".dbprint.yaml").write_text(config)
        conn = load_project(tmp_path).connections["primary"]
        Engine(MockAdapter(_contacts_fixture()), conn, tmp_path).generate()

        return yaml.safe_load(
            (
                tmp_path / "prints" / "primary" / "seedbank" / "collector" / "statistics.yaml"
            ).read_text(),
        )

    def test_a_defaults_rule_redacts_the_print(self, tmp_path: Path) -> None:
        payload = self._generate(
            tmp_path,
            "defaults:\n  redact:\n    - sensitivity: [contact]\n      with: drop\n"
            "connections:\n  primary:\n    adapter: postgres\n",
        )
        col = payload["columns"]["institution"]

        assert col["redacted"] == "drop"
        assert all("value" not in e for e in col["values"])

    def test_no_address_reaches_disk_under_a_defaults_rule(self, tmp_path: Path) -> None:
        self._generate(
            tmp_path,
            "defaults:\n  redact:\n    - sensitivity: [contact]\n      with: drop\n"
            "connections:\n  primary:\n    adapter: postgres\n",
        )
        written = (
            tmp_path / "prints" / "primary" / "seedbank" / "collector" / "statistics.yaml"
        ).read_text()

        assert "@" not in written

    def test_a_connection_rule_changes_the_primitive_a_defaults_rule_set(
        self,
        tmp_path: Path,
    ) -> None:
        payload = self._generate(
            tmp_path,
            "defaults:\n  redact:\n    - sensitivity: [contact]\n      with: mask\n"
            "connections:\n  primary:\n    adapter: postgres\n    redact:\n"
            "      - sensitivity: [contact]\n        with: drop\n",
        )

        assert payload["columns"]["institution"]["redacted"] == "drop"


class TestConformance:
    def test_a_redacted_print_validates(self, tmp_path: Path) -> None:
        from dbprint.conformance import validate_print

        conn = replace(
            _conn_config(tmp_path),
            redact=(RedactRule(columns=("*.herbarium_id",), with_="drop"),),
        )
        Engine(MockAdapter(_curator_fixture()), conn, tmp_path).generate()
        errors = [i for i in validate_print(tmp_path / "primary") if i.severity == "error"]

        assert errors == []

    def test_a_rule_reaching_a_valueless_column_emits_no_marker(self, tmp_path: Path) -> None:
        """A `json` or `unsupported` column has no cell values, so there is nothing to redact."""

        conn = replace(
            _conn_config(tmp_path),
            redact=(RedactRule(columns=("*",), with_="mask"),),
        )
        Engine(MockAdapter(_valueless_fixture()), conn, tmp_path).generate()
        columns = _shapes_payload(tmp_path)["columns"]

        assert columns["payload"]["classification"] == "json"
        assert columns["field_photo"]["classification"] == "unsupported"
        assert "redacted" not in columns["payload"]
        assert "redacted" not in columns["field_photo"]
        # The control: a column the matrix permits it on, under the same rule.
        assert columns["phone"]["redacted"] == "mask"

    def test_a_rule_reaching_a_prose_text_column_marks_it_and_lists_nothing(
        self,
        tmp_path: Path,
    ) -> None:
        """`field_notes` publishes no literal at all, and the marker still reports its cover."""

        conn = replace(
            _conn_config(tmp_path),
            redact=(RedactRule(columns=("*",), with_="mask"),),
        )
        Engine(MockAdapter(_prose_fixture()), conn, tmp_path).generate()
        columns = yaml.safe_load(
            (tmp_path / "primary" / "public" / "curator_note" / "statistics.yaml").read_text(),
        )["columns"]

        assert columns["field_notes"]["classification"] == "text"
        assert columns["field_notes"]["inferred"]["looks_like"] == "prose"
        assert columns["field_notes"]["redacted"] == "mask"
        assert "values" not in columns["field_notes"]
        assert "range" not in columns["field_notes"]
        assert "percentiles" not in columns["field_notes"]
        # The control: a non-prose column under the same rule keeps its value list.
        assert columns["institution"]["redacted"] == "mask"
        assert columns["institution"]["values"]

    def test_a_looks_like_targeted_rule_reaches_the_same_verdict(self, tmp_path: Path) -> None:
        """`looks_like: [prose]` matches `field_notes` and `status`; only the text one strips."""

        conn = replace(
            _conn_config(tmp_path),
            redact=(RedactRule(looks_like=("prose",), with_="mask"),),
        )
        Engine(MockAdapter(_prose_fixture()), conn, tmp_path).generate()
        columns = yaml.safe_load(
            (tmp_path / "primary" / "public" / "curator_note" / "statistics.yaml").read_text(),
        )["columns"]

        assert columns["field_notes"]["redacted"] == "mask"
        assert "values" not in columns["field_notes"]
        assert columns["status"]["redacted"] == "mask"
        assert columns["status"]["values"]

    def test_a_sensitivity_targeted_rule_reaches_the_same_verdict_too(
        self,
        tmp_path: Path,
    ) -> None:
        """The exemption follows the column: `phone` loses its list, `institution` keeps one."""

        conn = replace(
            _conn_config(tmp_path),
            redact=(RedactRule(sensitivity=("contact",), with_="mask"),),
        )
        Engine(MockAdapter(_prose_fixture()), conn, tmp_path).generate()
        columns = yaml.safe_load(
            (tmp_path / "primary" / "public" / "curator_note" / "statistics.yaml").read_text(),
        )["columns"]

        assert columns["phone"]["classification"] == "text"
        assert columns["phone"]["inferred"]["looks_like"] == "prose"
        assert columns["phone"]["inferred"]["sensitivity"] == "contact"
        assert columns["phone"]["redacted"] == "mask"
        assert "values" not in columns["phone"]
        assert columns["institution"]["inferred"]["sensitivity"] == "contact"
        assert columns["institution"]["redacted"] == "mask"
        assert columns["institution"]["values"]

    def test_a_rule_reaching_a_prose_categorical_column_keeps_its_marker(
        self,
        tmp_path: Path,
    ) -> None:
        """The prose exemption stops at `categorical`, which always carries a value list."""

        conn = replace(
            _conn_config(tmp_path),
            redact=(RedactRule(columns=("*",), with_="mask"),),
        )
        Engine(MockAdapter(_prose_fixture()), conn, tmp_path).generate()
        status = yaml.safe_load(
            (tmp_path / "primary" / "public" / "curator_note" / "statistics.yaml").read_text(),
        )["columns"]["status"]

        assert status["classification"] == "categorical"
        assert status["inferred"]["looks_like"] == "prose"
        assert status["redacted"] == "mask"
        assert status["values"]

    def test_a_print_carrying_name_only_sensitivity_validates(self, tmp_path: Path) -> None:
        """`phone` classifies numeric, never sampled, so `contact` comes from its name alone."""

        from dbprint.conformance import validate_print

        conn = _conn_config(tmp_path)
        Engine(MockAdapter(_valueless_fixture()), conn, tmp_path).generate()
        columns = _shapes_payload(tmp_path)["columns"]

        assert columns["phone"]["inferred"] == {"sensitivity": "contact"}

        errors = [i for i in validate_print(tmp_path / "primary") if i.severity == "error"]
        assert errors == [], "\n".join(f"  {e.code} at {e.path}: {e.detail}" for e in errors)


class TestACoveredColumnIsNeverSketched:
    """SPEC 2.2.14: a sketch is a set of digests of the very cell values a rule withheld."""

    @staticmethod
    def _columns(tmp_path: Path, **kwargs: Any) -> dict[str, Any]:
        conn = replace(_conn_config(tmp_path, enumeration_threshold=0), **kwargs)
        Engine(MockAdapter(_sketchable_prose_fixture()), conn, tmp_path).generate()

        return yaml.safe_load(
            (tmp_path / "primary" / "seedbank" / "collector" / "statistics.yaml").read_text(),
        )["columns"]

    def test_a_covered_prose_column_carries_no_sketch(self, tmp_path: Path) -> None:
        """`field_notes` publishes no literal, so only the marker can exclude it."""

        columns = self._columns(
            tmp_path,
            redact=(RedactRule(columns=("*.field_notes",), with_="mask"),),
        )

        assert columns["field_notes"]["redacted"] == "mask"
        assert "values" not in columns["field_notes"]
        assert "sketch" not in columns["field_notes"]
        assert columns["institution"]["sketch"]["method"] == "kmv_md5_lo64"
        assert "redacted" not in columns["institution"]

    @pytest.mark.parametrize("primitive", ["mask", "drop", "hash"])
    def test_every_primitive_withholds_the_sketch(
        self,
        tmp_path: Path,
        primitive: Primitive,
    ) -> None:
        columns = self._columns(
            tmp_path,
            redact=(RedactRule(columns=("*.field_notes",), with_=primitive),),
            redaction_salt="pepper",
        )

        assert columns["field_notes"]["redacted"] == primitive
        assert "sketch" not in columns["field_notes"]

    def test_the_flag_that_widens_the_population_does_not_widen_the_exclusion(
        self,
        tmp_path: Path,
    ) -> None:
        columns = self._columns(
            tmp_path,
            redact=(RedactRule(columns=("*.field_notes",), with_="mask"),),
            sketch_all_columns=True,
        )

        assert "sketch" not in columns["field_notes"]
        assert columns["institution"]["sketch"]["method"] == "kmv_md5_lo64"

    def test_a_covered_column_whose_value_read_failed_still_declares_its_cover(
        self,
        tmp_path: Path,
    ) -> None:
        """No literal survived to be redacted and none was measured either."""

        conn = replace(
            _conn_config(tmp_path),
            redact=(RedactRule(columns=("*.field_notes",), with_="mask"),),
        )
        Engine(MockAdapter(_unmeasured_values_fixture()), conn, tmp_path).generate()
        field_notes = yaml.safe_load(
            (tmp_path / "primary" / "seedbank" / "accession" / "statistics.yaml").read_text(),
        )["columns"]["field_notes"]

        assert field_notes["unmeasured"] == ["distribution", "values", "values_coverage"]
        assert field_notes["redacted"] == "mask"
        assert "sketch" not in field_notes


class TestBoundsUnderARedactedColumn:
    """A rule reaching a bound-bearing column, from `generate` through to `check`: `drop`
    omits `range` and `percentiles` outright, `mask` and `hash` substitute in place.
    """

    @pytest.mark.parametrize("primitive", ["mask", "hash", "drop"])
    def test_a_numeric_column_validates(self, tmp_path: Path, primitive: str) -> None:
        assert _redacted_print_errors(tmp_path, _valueless_fixture(), "*.phone", primitive) == []

    @pytest.mark.parametrize("primitive", ["mask", "hash", "drop"])
    def test_a_temporal_column_validates(self, tmp_path: Path, primitive: str) -> None:
        errors = _redacted_print_errors(tmp_path, _dated_fixture(), "*.observed_at", primitive)

        assert errors == []

    def test_drop_removes_the_bounds_and_the_others_keep_them(self, tmp_path: Path) -> None:
        """Where the three primitives part company, and the measurements that do not."""

        dropped = self._phone(tmp_path / "d", "drop")
        masked = self._phone(tmp_path / "m", "mask")

        assert "range" not in dropped
        assert "percentiles" not in dropped
        assert masked["range"]["min"] == MASK_PLACEHOLDER
        assert masked["percentiles"]
        assert dropped["cardinality"] == masked["cardinality"]
        assert dropped["distribution"] == masked["distribution"]

    def test_drop_removes_unrepresentable_too_and_mask_keeps_it(self, tmp_path: Path) -> None:
        """`unrepresentable` names `max`, so `drop` removing `range` must remove the marker
        too - a name pointing at a dropped bound fails conformance (SPEC 2.2.4).
        """

        dropped = self._observed_at(tmp_path / "d", "drop")
        masked = self._observed_at(tmp_path / "m", "mask")

        assert "range" not in dropped
        assert "unrepresentable" not in dropped
        assert masked["range"]["min"] == MASK_PLACEHOLDER
        assert masked["unrepresentable"] == ["max"]

    def test_a_sensitivity_targeted_rule_reaches_the_same_outcome(self, tmp_path: Path) -> None:
        """`phone` reports `contact` from its column name with no sample drawn, so a
        sensitivity rule's coverage cannot be known at load time.
        """

        from dbprint.conformance import validate_print

        conn = replace(
            _conn_config(tmp_path),
            redact=(RedactRule(sensitivity=("contact",), with_="drop"),),
        )
        Engine(MockAdapter(_valueless_fixture()), conn, tmp_path).generate()
        phone = _shapes_payload(tmp_path)["columns"]["phone"]

        assert phone["redacted"] == "drop"
        assert "range" not in phone
        assert "percentiles" not in phone
        assert [i for i in validate_print(tmp_path / "primary") if i.severity == "error"] == []

    def _phone(self, tmp_path: Path, primitive: str) -> dict[str, Any]:
        conn = replace(
            _conn_config(tmp_path),
            redact=(RedactRule(columns=("*.phone",), with_=primitive),),
        )
        Engine(MockAdapter(_valueless_fixture()), conn, tmp_path).generate()

        return _shapes_payload(tmp_path)["columns"]["phone"]

    def _observed_at(self, tmp_path: Path, primitive: str) -> dict[str, Any]:
        conn = replace(
            _conn_config(tmp_path),
            redact=(RedactRule(columns=("*.observed_at",), with_=primitive),),
            redaction_salt="pepper",
        )
        Engine(MockAdapter(_dated_fixture()), conn, tmp_path).generate()
        payload = yaml.safe_load(
            (
                tmp_path / "primary" / "seedbank" / "germination_trial" / "statistics.yaml"
            ).read_text(),
        )

        return payload["columns"]["observed_at"]


class TestAggregatesUnderARedactedColumn:
    """SPEC 2.2.3's redacted-aggregate footnote, end-to-end through `generate` - a redacted
    column with at most one non-null row must not publish an aggregate equal to that row.
    """

    def _amount(self, tmp_path: Path, *, values: tuple[int, ...]) -> dict[str, Any]:
        """`values` is the scanned rows themselves, so repeats decide `cardinality`."""

        counts = Counter(values)
        non_null = len(values)
        distinct = len(counts)
        total = float(sum(values))
        mean = total / non_null
        table = MockTable(
            type="table",
            namespace_path=("fixture", "staging"),
            ddl="CREATE TABLE fixture.staging (amount bigint);\n",
            columns=[
                ColumnMeta(
                    name="amount",
                    sql_type="bigint",
                    nullable=True,
                    default=None,
                    ordinal=1,
                ),
            ],
            relationships=[],
            indexes=[],
            comments=CommentsMeta(table=None, columns={}),
            stats={
                "amount": ColumnStats(
                    sql_type="bigint",
                    nullable=True,
                    null_count=0,
                    null_rate=0.0,
                    cardinality=distinct,
                    cardinality_ratio=round(distinct / non_null, 6),
                    cardinality_method="exact",
                    range=Range(min=min(values), max=max(values)),
                    percentiles={"p50": sorted(values)[non_null // 2]},
                    mean=mean,
                    sum=total,
                    zero_count=0,
                    negative_count=0,
                    quantized_count=non_null,
                    values=tuple(ValueCount(value=v, count=c) for v, c in counts.items()),
                    distribution="dominant_value" if distinct == 1 else "uniform",
                    frequencies=Frequencies(
                        top=max(counts.values()),
                        bottom=min(counts.values()),
                        listed=distinct,
                        total=non_null,
                    ),
                ),
            },
            samples={},
            row_count=non_null,
        )
        # enumeration_threshold=0 keeps `amount` out of the categorical bucket regardless of
        # cardinality, so both scenarios below classify `numeric` - the footnote's own axis.
        conn = replace(
            _conn_config(tmp_path, enumeration_threshold=0),
            redact=(RedactRule(columns=("*.amount",), with_="mask"),),
        )
        Engine(MockAdapter({"fixture.staging": table}), conn, tmp_path).generate()
        payload = yaml.safe_load(
            (tmp_path / "primary" / "fixture" / "staging" / "statistics.yaml").read_text(),
        )

        return payload["columns"]["amount"]

    def test_withheld_on_a_single_non_null_row(self, tmp_path: Path) -> None:
        amount = self._amount(tmp_path, values=(42,))

        assert amount["classification"] == "numeric"
        assert amount["redacted"] == "mask"
        assert "mean" not in amount
        assert "sum" not in amount
        # The bound itself is unaffected by this rule - `mask` substitutes it, never omits it.
        assert amount["range"]["min"] == MASK_PLACEHOLDER

    def test_survives_once_more_than_one_row_is_non_null(self, tmp_path: Path) -> None:
        amount = self._amount(tmp_path, values=(10, 20, 30))

        assert amount["classification"] == "numeric"
        assert amount["redacted"] == "mask"
        assert amount["mean"] == 20.0
        assert amount["sum"] == 60.0

    def _institution(self, tmp_path: Path, *, distinct: int) -> dict[str, Any]:
        rows = 480
        values = tuple(f"institute {i:012d}" for i in range(distinct))
        per_value = rows // distinct
        collector = MockTable(
            type="table",
            namespace_path=("seedbank", "collector"),
            ddl="CREATE TABLE seedbank.collector (institution text);\n",
            columns=[
                ColumnMeta(
                    name="institution",
                    sql_type="text",
                    nullable=False,
                    default=None,
                    ordinal=1,
                ),
            ],
            relationships=[],
            indexes=[],
            comments=CommentsMeta(table=None, columns={}),
            stats={
                "institution": ColumnStats(
                    sql_type="text",
                    nullable=False,
                    null_count=0,
                    null_rate=0.0,
                    cardinality=distinct,
                    cardinality_ratio=round(distinct / rows, 6),
                    cardinality_method="exact",
                    values=tuple(ValueCount(value=v, count=per_value) for v in values),
                    values_coverage=1.0,
                    distribution="dominant_value" if distinct == 1 else "uniform",
                    length=Length(min=23, max=23, avg=23.0, p95=23.0),
                    empty_count=0,
                ),
            },
            samples={},
            row_count=rows,
        )
        conn = replace(
            _conn_config(tmp_path),
            redact=(RedactRule(columns=("*.institution",), with_="mask"),),
        )
        Engine(MockAdapter({"seedbank.collector": collector}), conn, tmp_path).generate()
        payload = yaml.safe_load(
            (tmp_path / "primary" / "seedbank" / "collector" / "statistics.yaml").read_text(),
        )

        return payload["columns"]["institution"]

    def test_length_is_withheld_on_one_distinct_value(self, tmp_path: Path) -> None:
        """`min == max == avg` over one value is the withheld literal's own character count."""

        institution = self._institution(tmp_path, distinct=1)

        assert institution["classification"] == "categorical"
        assert institution["redacted"] == "mask"
        assert "length" not in institution

    def test_length_survives_on_two_distinct_values(self, tmp_path: Path) -> None:
        institution = self._institution(tmp_path, distinct=2)

        assert institution["redacted"] == "mask"
        assert institution["length"]["min"] == 23

    def test_withheld_on_many_rows_carrying_one_distinct_value(self, tmp_path: Path) -> None:
        """480 rows of one number: the mean is that number and the sum divides back to it."""

        amount = self._amount(tmp_path, values=(85,) * 480)

        assert amount["cardinality"] == 1
        assert amount["redacted"] == "mask"
        assert "mean" not in amount
        assert "sum" not in amount

    def test_survives_once_a_second_distinct_value_appears(self, tmp_path: Path) -> None:
        """The threshold is distinctness, not size: one more value and neither lands on it."""

        amount = self._amount(tmp_path, values=(85,) * 479 + (86,))

        assert amount["cardinality"] == 2
        assert amount["redacted"] == "mask"
        assert 85 < amount["mean"] < 86
        assert amount["sum"] > 85


class TestRedactedDayCounts:
    """A redacted temporal column's derived day counts are floored to 90, under every primitive.

    `_dated_fixture`'s values sit below the granularity, so these seed their own: 89 days
    (stale) floors to 0, which reads as `live` if `classification` is re-derived from the
    coarsened integer rather than the true age.
    """

    @pytest.mark.parametrize("primitive", ["mask", "hash", "drop"])
    def test_max_age_days_is_floored_to_90(self, tmp_path: Path, primitive: str) -> None:
        observed_at = self._observed_at(tmp_path, primitive)

        assert observed_at["freshness"]["max_age_days"] == 0

    @pytest.mark.parametrize("primitive", ["mask", "hash"])
    def test_span_days_is_floored_to_90(self, tmp_path: Path, primitive: str) -> None:
        """`drop` carries no `range` block at all - nothing to coarsen there."""

        observed_at = self._observed_at(tmp_path, primitive)

        assert observed_at["range"]["span_days"] == 360

    def test_classification_still_reads_the_true_age_not_the_coarsened_one(
        self,
        tmp_path: Path,
    ) -> None:
        """89 days is `stale`; floored to 0 it would misread as `live`."""

        observed_at = self._observed_at(tmp_path, "mask")

        assert observed_at["freshness"]["max_age_days"] == 0
        assert observed_at["freshness"]["classification"] == "stale"

    def test_an_unredacted_column_is_uncoarsened(self, tmp_path: Path) -> None:
        conn = replace(_conn_config(tmp_path), redact=())
        Engine(MockAdapter(_stale_dated_fixture()), conn, tmp_path).generate()
        observed_at = self._payload(tmp_path)

        assert observed_at["freshness"]["max_age_days"] == 89
        assert observed_at["range"]["span_days"] == 400

    def _observed_at(self, tmp_path: Path, primitive: str) -> dict[str, Any]:
        conn = replace(
            _conn_config(tmp_path),
            redact=(RedactRule(columns=("*.observed_at",), with_=primitive),),
            redaction_salt="pepper",
        )
        Engine(MockAdapter(_stale_dated_fixture()), conn, tmp_path).generate()

        return self._payload(tmp_path)

    def _payload(self, tmp_path: Path) -> dict[str, Any]:
        payload = yaml.safe_load(
            (
                tmp_path / "primary" / "seedbank" / "germination_trial" / "statistics.yaml"
            ).read_text(),
        )

        return payload["columns"]["observed_at"]


def _stale_dated_fixture() -> dict[str, MockTable]:
    """`_dated_fixture` with a `stale` age (89 days) and a 400-day span, both below their own
    coarsening boundary so coarsening changes them.

    `max_age_days` is engine-derived from `range.max` against the run's instant, so `max` is
    dated 89 days back; `span_days` is a bare override, asserted independently of `range.min`.
    """

    fixture = _dated_fixture()
    table = fixture["seedbank.germination_trial"]
    stats = table.stats["observed_at"]
    assert stats.range is not None
    stale_max = (datetime.now(UTC) - timedelta(days=89)).strftime("%Y-%m-%dT%H:%M:%SZ")
    coarsened = replace(
        stats,
        range=replace(stats.range, max=stale_max, span_days=400),
    )

    return {
        "seedbank.germination_trial": replace(
            table,
            stats={"observed_at": coarsened},
        ),
    }


def _redacted_print_errors(
    tmp_path: Path,
    fixture: dict[str, MockTable],
    columns_glob: str,
    primitive: str,
) -> list[Any]:
    """Generate under one rule and return the print's error-severity conformance issues."""

    from dbprint.conformance import validate_print

    conn = replace(
        _conn_config(tmp_path),
        redact=(RedactRule(columns=(columns_glob,), with_=primitive),),
        redaction_salt="pepper",
    )
    Engine(MockAdapter(fixture), conn, tmp_path).generate()

    return [i for i in validate_print(tmp_path / "primary") if i.severity == "error"]


def _shapes_payload(tmp_path: Path) -> dict[str, Any]:
    return yaml.safe_load(
        (tmp_path / "primary" / "fixture" / "contact_probe" / "statistics.yaml").read_text(),
    )


def _valueless_fixture() -> dict[str, MockTable]:
    """One table carrying the two classifications that hold no cell values, plus a control.

    `phone` is a bigint above the enumeration threshold, so it classifies numeric whatever
    its uniqueness (SPEC 4.2); its `contact` sensitivity comes from the name, no sample drawn.
    """

    return {
        "fixture.contact_probe": MockTable(
            type="table",
            namespace_path=("fixture", "contact_probe"),
            ddl="CREATE TABLE fixture.contact_probe (phone bigint, payload jsonb, field_photo bytea);\n",
            columns=[
                ColumnMeta(
                    name="phone",
                    sql_type="bigint",
                    nullable=False,
                    default=None,
                    ordinal=1,
                ),
                ColumnMeta(
                    name="payload",
                    sql_type="jsonb",
                    nullable=True,
                    default=None,
                    ordinal=2,
                ),
                ColumnMeta(
                    name="field_photo",
                    sql_type="bytea",
                    nullable=True,
                    default=None,
                    ordinal=3,
                ),
            ],
            relationships=[],
            indexes=[],
            comments=CommentsMeta(table=None, columns={}),
            stats={
                "phone": ColumnStats(
                    sql_type="bigint",
                    nullable=False,
                    null_count=0,
                    null_rate=0.0,
                    cardinality=80,
                    cardinality_ratio=0.8,
                    cardinality_method="exact",
                    range=Range(min=1, max=99),
                    percentiles={"p50": 50},
                    mean=49.5,
                    sum=4950.0,
                    zero_count=0,
                    negative_count=0,
                    quantized_count=80,
                    values=(ValueCount(value=50, count=2), ValueCount(value=51, count=1)),
                    distribution="uniform",
                    frequencies=Frequencies(top=2, bottom=1, listed=80, total=100),
                ),
                "payload": ColumnStats(
                    sql_type="jsonb",
                    nullable=True,
                    null_count=0,
                    null_rate=0.0,
                    cardinality=60,
                    cardinality_ratio=0.6,
                    cardinality_method="exact",
                ),
                "field_photo": ColumnStats(
                    sql_type="bytea",
                    nullable=True,
                    null_count=0,
                    null_rate=0.0,
                    cardinality=None,
                    cardinality_ratio=None,
                    cardinality_method=None,
                ),
            },
            samples={},
            row_count=100,
        ),
    }


def _dated_fixture() -> dict[str, MockTable]:
    """One temporal column, the other classification whose row carries bounds.

    Unlike numeric, `drop` keeps the `range.max`-derived `freshness`, and `range` carries
    `span_days`. `unrepresentable` names `max`, so `drop` is exercised against a field naming
    another; `max` sits a day before the run's instant, so `max_age_days` always reads 1.
    """

    now = datetime.now(UTC)
    observed_min = (now - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    observed_max = (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")

    return {
        "seedbank.germination_trial": MockTable(
            type="table",
            namespace_path=("seedbank", "germination_trial"),
            ddl="CREATE TABLE seedbank.germination_trial (observed_at timestamp);\n",
            columns=[
                ColumnMeta(
                    name="observed_at",
                    sql_type="timestamp",
                    nullable=False,
                    default=None,
                    ordinal=1,
                ),
            ],
            relationships=[],
            indexes=[],
            comments=CommentsMeta(table=None, columns={}),
            stats={
                "observed_at": ColumnStats(
                    sql_type="timestamp",
                    nullable=False,
                    null_count=0,
                    null_rate=0.0,
                    cardinality=60,
                    cardinality_ratio=0.6,
                    cardinality_method="exact",
                    range=Range(min=observed_min, max=observed_max, span_days=1),
                    percentiles={"p50": observed_max},
                    values=(
                        ValueCount(value=observed_max, count=2),
                        ValueCount(value=observed_min, count=1),
                    ),
                    distribution="uniform",
                    frequencies=Frequencies(top=2, bottom=1, listed=60, total=80),
                    unrepresentable=("max",),
                    quantized_count=0,
                ),
            },
            samples={},
            row_count=100,
        ),
    }


def _unmeasured_values_fixture() -> dict[str, MockTable]:
    """A text column whose value read failed: no literal published, and `unmeasured` says so."""

    return {
        "seedbank.accession": MockTable(
            type="table",
            namespace_path=("seedbank", "accession"),
            ddl="CREATE TABLE seedbank.accession (field_notes text);\n",
            columns=[
                ColumnMeta(
                    name="field_notes",
                    sql_type="text",
                    nullable=False,
                    default=None,
                    ordinal=1,
                ),
            ],
            relationships=[],
            indexes=[],
            comments=CommentsMeta(table=None, columns={}),
            stats={
                "field_notes": ColumnStats(
                    sql_type="text",
                    nullable=False,
                    null_count=0,
                    null_rate=0.0,
                    cardinality=100,
                    cardinality_ratio=0.5,
                    cardinality_method="exact",
                    values=None,
                    empty_count=0,
                    unmeasured=("values", "values_coverage", "distribution"),
                ),
            },
            samples={},
            row_count=200,
        ),
    }


def _sketchable_prose_fixture() -> dict[str, MockTable]:
    """Two text columns with exhaustive value lists, so both are sketched; one reads as prose."""

    notes = tuple(
        f"the specimen in tray {i} was rehoused and has recovered since" for i in range(20)
    )
    institutions = tuple(f"institute-{i:02d}" for i in range(20))

    def stats(values: tuple[str, ...], width: int) -> ColumnStats:
        return ColumnStats(
            sql_type="text",
            nullable=False,
            null_count=0,
            null_rate=0.0,
            cardinality=20,
            cardinality_ratio=0.5,
            cardinality_method="exact",
            values=tuple(ValueCount(value=v, count=2) for v in values),
            values_coverage=1.0,
            distribution="uniform",
            empty_count=0,
            length=Length(min=width, max=width, avg=float(width), p95=float(width)),
        )

    return {
        "seedbank.collector": MockTable(
            type="table",
            namespace_path=("seedbank", "collector"),
            ddl="CREATE TABLE seedbank.collector (field_notes text, institution text);\n",
            columns=[
                ColumnMeta(
                    name="field_notes",
                    sql_type="text",
                    nullable=False,
                    default=None,
                    ordinal=1,
                ),
                ColumnMeta(
                    name="institution",
                    sql_type="text",
                    nullable=False,
                    default=None,
                    ordinal=2,
                ),
            ],
            relationships=[],
            indexes=[],
            comments=CommentsMeta(table=None, columns={}),
            stats={
                "field_notes": stats(notes, 58),
                "institution": stats(institutions, 13),
            },
            samples={"field_notes": list(notes), "institution": list(institutions)},
            row_count=40,
        ),
    }
