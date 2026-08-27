"""Negative tests: one per error-catalog code from SPEC 6.3.

Each mutates a copy of the reference example and asserts its code appears; others may fire too.
"""

from __future__ import annotations

import base64
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

from dbprint.conformance import Issue, validate_print
from dbprint.conformance.schema_validation import check_relationships


EXAMPLE = (
    Path(__file__).resolve().parents[2] / "docs/format/v1/examples/production/prints/production"
)


@pytest.fixture
def print_dir(tmp_path: Path) -> Path:
    """Writable copy of the reference example."""

    dst = tmp_path / "production"
    shutil.copytree(EXAMPLE, dst)

    return dst


def _codes(issues: list[Issue]) -> set[str]:
    return {i.code for i in issues}


# YAML helpers that preserve string timestamps across roundtrip


class _StringTimestampLoader(yaml.SafeLoader):
    pass


def _construct_timestamp_as_string(loader, node):
    return loader.construct_scalar(node)


_StringTimestampLoader.add_constructor(
    "tag:yaml.org,2002:timestamp",
    _construct_timestamp_as_string,
)


def _load_yaml_file(p: Path) -> Any:
    return yaml.load(p.read_text(), Loader=_StringTimestampLoader)


def _write_yaml_file(p: Path, data: Any) -> None:
    p.write_text(
        yaml.safe_dump(data, default_flow_style=False, sort_keys=False, allow_unicode=True),
    )


# --- Layout ---------------------------------------------------------


def test_layout_missing_manifest(print_dir: Path) -> None:
    (print_dir / "manifest.yaml").unlink()
    assert "layout.missing-manifest" in _codes(validate_print(print_dir))


def test_layout_invalid_path_segment(print_dir: Path) -> None:
    bad = print_dir / "fixture" / "PublicUpper" / "curator" / "ddl.sql"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("-- bad\n")
    assert "layout.invalid-path-segment" in _codes(validate_print(print_dir))


def test_layout_unknown_file(print_dir: Path) -> None:
    (print_dir / "seedbank/accession/notes.txt").write_text("foo\n")
    assert "layout.unknown-file" in _codes(validate_print(print_dir))


def test_layout_unexpected_directory_level(print_dir: Path) -> None:
    bad_dir = print_dir / "fixture" / "extra_level"
    bad_dir.mkdir(parents=True)
    (bad_dir / "ddl.sql").write_text("-- bad\n")
    assert "layout.unexpected-directory-level" in _codes(validate_print(print_dir))


def test_layout_missing_reading_guide(print_dir: Path) -> None:
    (print_dir / "reading.md").unlink()
    assert "layout.missing-reading-guide" in _codes(validate_print(print_dir))


def test_layout_missing_diff(print_dir: Path) -> None:
    (print_dir / "diff.yaml").unlink()
    assert "layout.missing-diff" in _codes(validate_print(print_dir))


def test_layout_diff_not_required_before_any_table_is_recorded(tmp_path: Path) -> None:
    root = tmp_path / "empty"
    root.mkdir()
    (root / "manifest.yaml").write_text("format_version: 1\ngenerated_at: '2020-01-01T00:00:00Z'\n")
    (root / "reading.md").write_text("# Reading a dbprint print\n")
    assert "layout.missing-diff" not in _codes(validate_print(root))


# --- Schema / YAML validity -----------------------------------------


def test_schema_invalid_yaml(print_dir: Path) -> None:
    target = print_dir / "manifest.yaml"
    target.write_text("not: valid: yaml: :")
    assert "schema.invalid-yaml" in _codes(validate_print(print_dir))


def test_schema_missing_required_field(print_dir: Path) -> None:
    target = print_dir / "manifest.yaml"
    data = _load_yaml_file(target)
    del data["connection"]
    _write_yaml_file(target, data)
    assert "schema.missing-required-field" in _codes(validate_print(print_dir))


def test_schema_type_mismatch(print_dir: Path) -> None:
    target = print_dir / "manifest.yaml"
    data = _load_yaml_file(target)
    data["dbprint_version"] = 42
    _write_yaml_file(target, data)
    assert "schema.type-mismatch" in _codes(validate_print(print_dir))


def test_schema_invalid_percentile_key(print_dir: Path) -> None:
    target = print_dir / "seedbank/accession/statistics.yaml"
    data = _load_yaml_file(target)
    data["columns"]["received_at"]["percentiles"]["p100"] = "2026-05-15T22:42:08Z"
    _write_yaml_file(target, data)
    assert "schema.invalid-percentile-key" in _codes(validate_print(print_dir))


def test_schema_unknown_classification(print_dir: Path) -> None:
    target = print_dir / "seedbank/accession/statistics.yaml"
    data = _load_yaml_file(target)
    data["columns"]["accession_id"]["classification"] = "weird_new_value"
    _write_yaml_file(target, data)
    assert "schema.unknown-classification" in _codes(validate_print(print_dir))


def test_schema_unknown_change_kind(print_dir: Path) -> None:
    target = print_dir / "diff.yaml"
    data = _load_yaml_file(target)
    data["changes"].append({"kind": "weird_new_kind", "table": "a"})
    _write_yaml_file(target, data)
    assert "schema.unknown-change-kind" in _codes(validate_print(print_dir))


def test_schema_unknown_looks_like(print_dir: Path) -> None:
    target = print_dir / "seedbank/accession/statistics.yaml"
    data = _load_yaml_file(target)
    data["columns"]["accession_id"]["inferred"]["looks_like"] = "weird_pattern"
    _write_yaml_file(target, data)
    assert "schema.unknown-looks-like" in _codes(validate_print(print_dir))


def test_a_print_carrying_ipv4_warns_not_errors(print_dir: Path) -> None:
    """A pattern outside the current vocabulary makes a print stale, not invalid (SPEC 5.3)."""

    target = print_dir / "seedbank/accession/statistics.yaml"
    data = _load_yaml_file(target)
    data["columns"]["accession_id"]["inferred"]["looks_like"] = "ipv4"
    _write_yaml_file(target, data)
    issues = validate_print(print_dir)
    matching = [i for i in issues if i.code == "schema.unknown-looks-like"]

    assert matching, "no schema.unknown-looks-like issue was raised"
    assert all(i.severity == "warning" for i in matching)


def test_schema_unknown_sensitivity(print_dir: Path) -> None:
    target = print_dir / "seedbank/accession/statistics.yaml"
    data = _load_yaml_file(target)
    data["columns"]["accession_id"]["inferred"]["sensitivity"] = "weird_category"
    _write_yaml_file(target, data)
    assert "schema.unknown-sensitivity" in _codes(validate_print(print_dir))


# --- Format version -------------------------------------------------


def test_version_missing_format_version(print_dir: Path) -> None:
    target = print_dir / "manifest.yaml"
    data = _load_yaml_file(target)
    del data["format_version"]
    _write_yaml_file(target, data)
    assert "version.missing-format-version" in _codes(validate_print(print_dir))


def test_version_invalid_format_version(print_dir: Path) -> None:
    target = print_dir / "manifest.yaml"
    data = _load_yaml_file(target)
    data["format_version"] = "one"
    _write_yaml_file(target, data)
    assert "version.invalid-format-version" in _codes(validate_print(print_dir))


def test_version_unknown_format_version(print_dir: Path) -> None:
    target = print_dir / "manifest.yaml"
    data = _load_yaml_file(target)
    data["format_version"] = 99
    _write_yaml_file(target, data)
    assert "version.unknown-format-version" in _codes(validate_print(print_dir))


# --- Manifest cross-checks ------------------------------------------


def test_manifest_missing_artifact(print_dir: Path) -> None:
    (print_dir / "seedbank/accession/ddl.sql").unlink()
    assert "manifest.missing-artifact" in _codes(validate_print(print_dir))


def test_manifest_orphaned_artifact(print_dir: Path) -> None:
    orphan_dir = print_dir / "fixture" / "public" / "ghost_table"
    orphan_dir.mkdir(parents=True)
    (orphan_dir / "ddl.sql").write_text("-- ghost\n")
    assert "manifest.orphaned-artifact" in _codes(validate_print(print_dir))


def test_manifest_annotations_claimed_but_missing(print_dir: Path) -> None:
    """SPEC 2.7.3: `manifest_annotations` claims a connection-root file that must exist."""

    manifest_path = print_dir / "manifest.yaml"
    manifest = _load_yaml_file(manifest_path)
    manifest["manifest_annotations"] = "manifest.annotations.yaml"
    _write_yaml_file(manifest_path, manifest)
    (print_dir / "manifest.annotations.yaml").unlink(missing_ok=True)

    assert "manifest.missing-artifact" in _codes(validate_print(print_dir))


def test_manifest_orphaned_artifact_in_a_declared_directory(print_dir: Path) -> None:
    """The table entry is declared; only this one artifact key is missing from it."""

    target = print_dir / "manifest.yaml"
    data = _load_yaml_file(target)
    del data["tables"]["seedbank.collector"]["artifacts"]["description"]
    _write_yaml_file(target, data)

    assert "manifest.orphaned-artifact" in _codes(validate_print(print_dir))


def test_manifest_missing_artifact_still_fires_beside_the_orphan_check(print_dir: Path) -> None:
    """The mirror direction - a declared entry whose file is absent - is unaffected."""

    (print_dir / "seedbank/collector/description.md").unlink()
    assert "manifest.missing-artifact" in _codes(validate_print(print_dir))


def test_manifest_table_fqn_mismatch(print_dir: Path) -> None:
    target = print_dir / "seedbank/accession/statistics.yaml"
    data = _load_yaml_file(target)
    data["table"] = "wrong.table.name"
    _write_yaml_file(target, data)
    assert "manifest.table-fqn-mismatch" in _codes(validate_print(print_dir))


def test_manifest_columns_count_mismatch(print_dir: Path) -> None:
    target = print_dir / "manifest.yaml"
    data = _load_yaml_file(target)
    data["tables"]["seedbank.accession"]["columns"] += 1
    _write_yaml_file(target, data)
    assert "manifest.columns-count-mismatch" in _codes(validate_print(print_dir))


def test_manifest_max_age_days_wrong_type(print_dir: Path) -> None:
    """The recorded threshold is a count of days; the schema is what says so."""

    target = print_dir / "manifest.yaml"
    data = _load_yaml_file(target)
    data["tables"]["seedbank.accession"]["max_age_days"] = "thirty"
    _write_yaml_file(target, data)

    assert "schema.type-mismatch" in _codes(validate_print(print_dir))


def test_manifest_max_age_days_negative(print_dir: Path) -> None:
    """SPEC 2.5 bounds the recorded threshold; below zero no print can satisfy it."""

    target = print_dir / "manifest.yaml"
    data = _load_yaml_file(target)
    data["tables"]["seedbank.accession"]["max_age_days"] = -7
    _write_yaml_file(target, data)

    issues = validate_print(print_dir)
    match = [
        i
        for i in issues
        if i.code == "schema.type-mismatch"
        and i.path == "manifest.yaml::tables.seedbank.accession.max_age_days"
    ]

    assert match, _codes(issues)
    assert match[0].severity == "error"


def test_manifest_max_age_days_zero_is_conformant(print_dir: Path) -> None:
    """Zero is the threshold of a table re-read on every run, not a violation."""

    target = print_dir / "manifest.yaml"
    data = _load_yaml_file(target)
    data["tables"]["seedbank.accession"]["max_age_days"] = 0
    _write_yaml_file(target, data)

    assert [i for i in validate_print(print_dir) if i.severity == "error"] == []


def test_manifest_without_a_recorded_threshold_is_conformant(print_dir: Path) -> None:
    target = print_dir / "manifest.yaml"
    data = _load_yaml_file(target)

    for entry in data["tables"].values():
        del entry["max_age_days"]

    _write_yaml_file(target, data)

    assert [i for i in validate_print(print_dir) if i.severity == "error"] == []


def test_manifest_missing_statistics_params_errors(print_dir: Path) -> None:
    target = print_dir / "manifest.yaml"
    data = _load_yaml_file(target)
    del data["statistics_params"]
    _write_yaml_file(target, data)

    issues = validate_print(print_dir)
    match = [i for i in issues if i.code == "schema.missing-required-field"]

    assert match, _codes(issues)
    assert match[0].severity == "error"


def test_manifest_missing_selectors_errors(print_dir: Path) -> None:
    target = print_dir / "manifest.yaml"
    data = _load_yaml_file(target)
    del data["selectors"]
    _write_yaml_file(target, data)

    assert "schema.missing-required-field" in _codes(validate_print(print_dir))


def test_manifest_missing_redaction_rules_configured_errors(print_dir: Path) -> None:
    target = print_dir / "manifest.yaml"
    data = _load_yaml_file(target)
    del data["redaction_rules_configured"]
    _write_yaml_file(target, data)

    assert "schema.missing-required-field" in _codes(validate_print(print_dir))


def test_manifest_selectors_mismatch_diff_errors(print_dir: Path) -> None:
    target = print_dir / "manifest.yaml"
    data = _load_yaml_file(target)
    data["selectors"] = {"include": ["only_this.*"], "exclude": []}
    _write_yaml_file(target, data)

    issues = validate_print(print_dir)
    match = [i for i in issues if i.code == "manifest.selectors-mismatch-diff"]

    assert match, _codes(issues)
    assert match[0].severity == "error"


# --- Statistics invariants ------------------------------------------


def test_stats_missing_required_field_for_classification(print_dir: Path) -> None:
    target = print_dir / "seedbank/accession/statistics.yaml"
    data = _load_yaml_file(target)
    # provenance_country is categorical; values is required
    del data["columns"]["provenance_country"]["values"]
    _write_yaml_file(target, data)
    assert "stats.missing-required-field-for-classification" in _codes(validate_print(print_dir))


def test_stats_forbidden_field_for_classification(print_dir: Path) -> None:
    target = print_dir / "seedbank/accession/statistics.yaml"
    data = _load_yaml_file(target)
    # accession_id is numeric (unique, SPEC 4.2); values is MUST NOT emit
    data["columns"]["accession_id"]["values"] = {"foo": 1}
    _write_yaml_file(target, data)
    assert "stats.forbidden-field-for-classification" in _codes(validate_print(print_dir))


def test_stats_physical_name_matches_key(print_dir: Path) -> None:
    target = print_dir / "seedbank/accession/statistics.yaml"
    data = _load_yaml_file(target)
    data["columns"]["accession_id"]["physical_name"] = "accession_id"
    _write_yaml_file(target, data)
    assert "stats.physical-name-matches-key" in _codes(validate_print(print_dir))


def test_stats_cardinality_exceeds_row_count(print_dir: Path) -> None:
    target = print_dir / "seedbank/accession/statistics.yaml"
    data = _load_yaml_file(target)
    data["columns"]["accession_id"]["cardinality"] = 999999999
    _write_yaml_file(target, data)
    assert "stats.cardinality-exceeds-row-count" in _codes(validate_print(print_dir))


def test_stats_null_count_exceeds_row_count(print_dir: Path) -> None:
    target = print_dir / "seedbank/accession/statistics.yaml"
    data = _load_yaml_file(target)
    data["columns"]["taxon_id"]["null_count"] = 999999999
    _write_yaml_file(target, data)
    assert "stats.null-count-exceeds-row-count" in _codes(validate_print(print_dir))


def test_stats_values_sum_mismatch(print_dir: Path) -> None:
    """Warning, not error - phase A and phase B are measured seconds apart on a live table."""

    target = print_dir / "seedbank/accession/statistics.yaml"
    data = _load_yaml_file(target)
    data["columns"]["provenance_country"]["values"][0]["count"] = 1
    _write_yaml_file(target, data)
    issues = validate_print(print_dir)
    assert "stats.values-sum-mismatch" in _codes(issues)
    assert next(i for i in issues if i.code == "stats.values-sum-mismatch").severity == "warning"


def test_stats_values_coverage_mismatch(print_dir: Path) -> None:
    """Warning, not error - same cross-phase drift `stats.values-sum-mismatch` reports."""

    target = print_dir / "seedbank/accession/statistics.yaml"
    data = _load_yaml_file(target)
    data["columns"]["taxon_id"]["values_coverage"] = 0.5
    _write_yaml_file(target, data)
    issues = validate_print(print_dir)
    assert "stats.values-coverage-mismatch" in _codes(issues)
    assert (
        next(i for i in issues if i.code == "stats.values-coverage-mismatch").severity == "warning"
    )


def test_stats_values_list_short_of_cardinality(print_dir: Path) -> None:
    target = print_dir / "seedbank/accession/statistics.yaml"
    data = _load_yaml_file(target)
    del data["columns"]["provenance_country"]["values"][0]
    _write_yaml_file(target, data)
    issues = validate_print(print_dir)
    assert "stats.values-list-short-of-cardinality" in _codes(issues)
    match = next(i for i in issues if i.code == "stats.values-list-short-of-cardinality")
    assert match.severity == "error"


def test_stats_values_list_exceeds_cardinality(print_dir: Path) -> None:
    target = print_dir / "seedbank/accession/statistics.yaml"
    data = _load_yaml_file(target)
    extra = dict(data["columns"]["provenance_country"]["values"][0])
    extra["value"] = "ZZ"
    data["columns"]["provenance_country"]["values"].append(extra)
    _write_yaml_file(target, data)
    issues = validate_print(print_dir)
    assert "stats.values-list-exceeds-cardinality" in _codes(issues)
    match = next(i for i in issues if i.code == "stats.values-list-exceeds-cardinality")
    assert match.severity == "warning"


def test_stats_values_list_length_mismatch_ignored_when_cardinality_is_approximate(
    print_dir: Path,
) -> None:
    """An HLL estimate may legitimately disagree with an entry count it never measured."""

    target = print_dir / "seedbank/accession/statistics.yaml"
    data = _load_yaml_file(target)
    del data["columns"]["provenance_country"]["values"][0]
    data["columns"]["provenance_country"]["cardinality_method"] = "approximate"
    _write_yaml_file(target, data)
    codes = _codes(validate_print(print_dir))
    assert "stats.values-list-short-of-cardinality" not in codes
    assert "stats.values-list-exceeds-cardinality" not in codes


def test_stats_values_list_short_of_cardinality_under_bounded_coverage(print_dir: Path) -> None:
    """The producer already disclosed the disagreement (SPEC 2.2.4) - warning, distinct code."""

    target = print_dir / "seedbank/accession/statistics.yaml"
    data = _load_yaml_file(target)
    del data["columns"]["provenance_country"]["values"][0]
    data["columns"]["provenance_country"]["values_coverage_method"] = "bounded"
    _write_yaml_file(target, data)
    issues = validate_print(print_dir)
    codes = _codes(issues)

    assert "stats.values-list-short-of-cardinality" not in codes
    match = next(i for i in issues if i.code == "stats.values-list-short-of-cardinality-bounded")
    assert match.severity == "warning"


def test_stats_values_list_short_of_cardinality_under_measured_coverage_is_unchanged(
    print_dir: Path,
) -> None:
    """An explicit `measured` marker is the undisclosed case - error, same as today."""

    target = print_dir / "seedbank/accession/statistics.yaml"
    data = _load_yaml_file(target)
    del data["columns"]["provenance_country"]["values"][0]
    data["columns"]["provenance_country"]["values_coverage_method"] = "measured"
    _write_yaml_file(target, data)
    issues = validate_print(print_dir)
    codes = _codes(issues)

    assert "stats.values-list-short-of-cardinality-bounded" not in codes
    match = next(i for i in issues if i.code == "stats.values-list-short-of-cardinality")
    assert match.severity == "error"


def test_stats_values_list_short_of_cardinality_with_no_method_field_is_unchanged(
    print_dir: Path,
) -> None:
    """An artifact carrying no method field at all is not `bounded` (SPEC 7.2's absence rule)."""

    target = print_dir / "seedbank/accession/statistics.yaml"
    data = _load_yaml_file(target)
    del data["columns"]["provenance_country"]["values"][0]
    data["columns"]["provenance_country"].pop("values_coverage_method", None)  # predates it
    _write_yaml_file(target, data)
    issues = validate_print(print_dir)
    codes = _codes(issues)

    assert "stats.values-list-short-of-cardinality-bounded" not in codes
    match = next(i for i in issues if i.code == "stats.values-list-short-of-cardinality")
    assert match.severity == "error"


def test_stats_values_list_exceeds_cardinality_under_bounded_is_not_double_downgraded(
    print_dir: Path,
) -> None:
    """`stats.values-list-exceeds-cardinality` is already a warning; `bounded` changes nothing."""

    target = print_dir / "seedbank/accession/statistics.yaml"
    data = _load_yaml_file(target)
    extra = dict(data["columns"]["provenance_country"]["values"][0])
    extra["value"] = "ZZ"
    data["columns"]["provenance_country"]["values"].append(extra)
    data["columns"]["provenance_country"]["values_coverage_method"] = "bounded"
    _write_yaml_file(target, data)
    issues = validate_print(print_dir)
    match = next(i for i in issues if i.code == "stats.values-list-exceeds-cardinality")

    assert match.severity == "warning"


def test_stats_null_rate_mismatch(print_dir: Path) -> None:
    target = print_dir / "seedbank/accession/statistics.yaml"
    data = _load_yaml_file(target)
    data["columns"]["provenance_country"]["null_rate"] = 0.5
    _write_yaml_file(target, data)
    issues = validate_print(print_dir)
    assert "stats.null-rate-mismatch" in _codes(issues)
    assert next(i for i in issues if i.code == "stats.null-rate-mismatch").severity == "error"


def test_stats_cardinality_ratio_mismatch(print_dir: Path) -> None:
    target = print_dir / "seedbank/accession/statistics.yaml"
    data = _load_yaml_file(target)
    data["columns"]["provenance_country"]["cardinality_ratio"] = 1.0
    _write_yaml_file(target, data)
    issues = validate_print(print_dir)
    assert "stats.cardinality-ratio-mismatch" in _codes(issues)
    match = next(i for i in issues if i.code == "stats.cardinality-ratio-mismatch")
    assert match.severity == "error"


def test_stats_a_floored_null_rate_agrees_with_the_validator(print_dir: Path) -> None:
    """A nonzero null_count under a huge scan floors to 0.000001, not the raw 0.0.

    The validator recomputes through the same producer rule, so the floored value must not
    trip stats.null-rate-mismatch; the scope breaks other columns, so only this one is read.
    """

    target = print_dir / "seedbank/accession/statistics.yaml"
    data = _load_yaml_file(target)
    data["scope"] = {"rows_scanned": 10_000_000}
    col = data["columns"]["provenance_country"]
    col["null_count"] = 1
    col["null_rate"] = 0.000001  # floored; round(1 / 10_000_000, 6) alone is 0.0
    _write_yaml_file(target, data)

    col_path = "seedbank/accession/statistics.yaml::columns.provenance_country"
    codes = {i.code for i in validate_print(print_dir) if i.path == col_path}
    assert "stats.null-rate-mismatch" not in codes


def test_stats_null_rate_and_cardinality_ratio_recompute_against_rows_scanned(
    print_dir: Path,
) -> None:
    """A narrowed read is checked against `scope.rows_scanned`, not `row_count`.

    Codes are read for `traits`'s own path: every other column still assumes a full read
    and legitimately trips other invariants once `rows_scanned` shrinks.
    """

    target = print_dir / "seedbank/accession/statistics.yaml"
    data = _load_yaml_file(target)
    col = data["columns"]["traits"]  # null_count=147, cardinality=80
    scanned = 1250  # half of row_count=2500, above both of traits' counts
    data["scope"] = {"rows_scanned": scanned}
    col["cardinality_ratio"] = round(col["cardinality"] / scanned, 6)
    col["null_rate"] = round(col["null_count"] / scanned, 6)
    _write_yaml_file(target, data)

    traits_path = "seedbank/accession/statistics.yaml::columns.traits"
    codes = {i.code for i in validate_print(print_dir) if i.path == traits_path}
    assert "stats.null-rate-mismatch" not in codes
    assert "stats.cardinality-ratio-mismatch" not in codes


def test_stats_values_not_ordered(print_dir: Path) -> None:
    target = print_dir / "seedbank/accession/statistics.yaml"
    data = _load_yaml_file(target)
    data["columns"]["provenance_country"]["values"].reverse()
    _write_yaml_file(target, data)
    assert "stats.values-not-ordered" in _codes(validate_print(print_dir))


def test_stats_percentiles_not_ordered_reversed(print_dir: Path) -> None:
    target = print_dir / "seedbank/accession/statistics.yaml"
    data = _load_yaml_file(target)
    percentiles = data["columns"]["viability_pct"]["percentiles"]
    percentiles["p01"], percentiles["p99"] = percentiles["p99"], percentiles["p01"]
    _write_yaml_file(target, data)
    issues = validate_print(print_dir)
    assert "stats.percentiles-not-ordered" in _codes(issues)
    match = next(i for i in issues if i.code == "stats.percentiles-not-ordered")
    assert match.severity == "error"


def test_stats_percentiles_not_ordered_single_inversion_temporal(print_dir: Path) -> None:
    """Ordering compares parsed instants, not string form - the rest stay ascending."""

    target = print_dir / "seedbank/accession/statistics.yaml"
    data = _load_yaml_file(target)
    percentiles = data["columns"]["received_at"]["percentiles"]
    percentiles["p50"], percentiles["p75"] = percentiles["p75"], percentiles["p50"]
    _write_yaml_file(target, data)
    assert "stats.percentiles-not-ordered" in _codes(validate_print(print_dir))


def test_stats_percentiles_equal_on_a_constant_column_is_conformant(print_dir: Path) -> None:
    """Non-decreasing, not strictly ascending - a single-valued column is legal."""

    target = print_dir / "seedbank/accession/statistics.yaml"
    data = _load_yaml_file(target)
    data["columns"]["viability_pct"]["percentiles"] = {
        "p01": 59.33,
        "p25": 59.33,
        "p50": 59.33,
        "p75": 59.33,
        "p99": 59.33,
    }
    _write_yaml_file(target, data)
    assert "stats.percentiles-not-ordered" not in _codes(validate_print(print_dir))


def test_stats_percentiles_a_non_default_configured_list_orders_correctly(
    print_dir: Path,
) -> None:
    target = print_dir / "seedbank/accession/statistics.yaml"
    data = _load_yaml_file(target)
    data["columns"]["viability_pct"]["percentiles"] = {"p10": 30.0, "p50": 59.33, "p90": 95.0}
    _write_yaml_file(target, data)
    assert "stats.percentiles-not-ordered" not in _codes(validate_print(print_dir))


def test_stats_percentile_outside_range_numeric(print_dir: Path) -> None:
    target = print_dir / "seedbank/accession/statistics.yaml"
    data = _load_yaml_file(target)
    data["columns"]["viability_pct"]["percentiles"]["p99"] = 999.0
    _write_yaml_file(target, data)
    issues = validate_print(print_dir)
    assert "stats.percentile-outside-range" in _codes(issues)
    match = next(i for i in issues if i.code == "stats.percentile-outside-range")
    assert match.severity == "error"


def test_stats_percentile_outside_range_temporal(print_dir: Path) -> None:
    target = print_dir / "seedbank/accession/statistics.yaml"
    data = _load_yaml_file(target)
    data["columns"]["received_at"]["percentiles"]["p99"] = "2030-01-01T00:00:00Z"
    _write_yaml_file(target, data)
    assert "stats.percentile-outside-range" in _codes(validate_print(print_dir))


def test_stats_percentile_containment_skipped_when_range_absent(print_dir: Path) -> None:
    """A `drop` marker leaves `range` absent while `percentiles` may remain."""

    target = print_dir / "seedbank/accession/statistics.yaml"
    data = _load_yaml_file(target)
    del data["columns"]["viability_pct"]["range"]
    data["columns"]["viability_pct"]["percentiles"]["p99"] = 999.0
    _write_yaml_file(target, data)
    assert "stats.percentile-outside-range" not in _codes(validate_print(print_dir))


def test_stats_percentile_containment_skipped_under_any_redacted_marker(
    print_dir: Path,
) -> None:
    """A redaction placeholder carries no literal to compare against `range`."""

    target = print_dir / "seedbank/accession/statistics.yaml"
    data = _load_yaml_file(target)
    data["columns"]["viability_pct"]["percentiles"]["p99"] = 999.0
    data["columns"]["viability_pct"]["redacted"] = "hash"
    _write_yaml_file(target, data)
    assert "stats.percentile-outside-range" not in _codes(validate_print(print_dir))


def test_stats_null_patterns_absent_with_nulls(print_dir: Path) -> None:
    target = print_dir / "seedbank/accession/statistics.yaml"
    data = _load_yaml_file(target)
    del data["null_patterns"]
    _write_yaml_file(target, data)

    assert "stats.null-patterns-absent-with-nulls" in _codes(validate_print(print_dir))


def test_stats_null_patterns_present_without_nulls(print_dir: Path) -> None:
    """The same code both ways: the block's presence is a claim about the data."""

    target = print_dir / "seedbank/collector/statistics.yaml"
    data = _load_yaml_file(target)
    data["null_patterns"] = {"coverage": 1.0, "patterns": []}
    _write_yaml_file(target, data)

    assert "stats.null-patterns-absent-with-nulls" in _codes(validate_print(print_dir))


def test_stats_null_patterns_unknown_column(print_dir: Path) -> None:
    target = print_dir / "seedbank/accession/statistics.yaml"
    data = _load_yaml_file(target)
    data["null_patterns"]["patterns"][0]["columns"] = ["not_a_column"]
    _write_yaml_file(target, data)

    assert "stats.null-patterns-unknown-column" in _codes(validate_print(print_dir))


def test_stats_null_patterns_sum_exceeds_rows_scanned(print_dir: Path) -> None:
    target = print_dir / "seedbank/accession/statistics.yaml"
    data = _load_yaml_file(target)
    data["null_patterns"]["patterns"][0]["count"] = data["row_count"] + 1
    _write_yaml_file(target, data)

    assert "stats.null-patterns-sum-exceeds-rows-scanned" in _codes(validate_print(print_dir))


def test_stats_null_patterns_sum_exceeds_rows_scanned_under_bounded_coverage(
    print_dir: Path,
) -> None:
    """The producer already disclosed the disagreement (SPEC 2.2.10) - warning, distinct code."""

    target = print_dir / "seedbank/accession/statistics.yaml"
    data = _load_yaml_file(target)
    data["null_patterns"]["patterns"][0]["count"] = data["row_count"] + 1
    data["null_patterns"]["coverage_method"] = "bounded"
    _write_yaml_file(target, data)
    codes = _codes(validate_print(print_dir))

    assert "stats.null-patterns-sum-exceeds-rows-scanned" not in codes
    assert "stats.null-patterns-sum-exceeds-rows-scanned-bounded" in codes


def test_stats_null_patterns_sum_exceeds_rows_scanned_under_measured_is_unchanged(
    print_dir: Path,
) -> None:
    target = print_dir / "seedbank/accession/statistics.yaml"
    data = _load_yaml_file(target)
    data["null_patterns"]["patterns"][0]["count"] = data["row_count"] + 1
    data["null_patterns"]["coverage_method"] = "measured"
    _write_yaml_file(target, data)
    codes = _codes(validate_print(print_dir))

    assert "stats.null-patterns-sum-exceeds-rows-scanned-bounded" not in codes
    assert "stats.null-patterns-sum-exceeds-rows-scanned" in codes


def test_stats_null_patterns_coverage_mismatch(print_dir: Path) -> None:
    target = print_dir / "seedbank/accession/statistics.yaml"
    data = _load_yaml_file(target)
    data["null_patterns"]["coverage"] = 0.5
    _write_yaml_file(target, data)

    assert "stats.null-patterns-coverage-mismatch" in _codes(validate_print(print_dir))


def test_stats_null_patterns_not_ordered(print_dir: Path) -> None:
    target = print_dir / "seedbank/accession/statistics.yaml"
    data = _load_yaml_file(target)
    data["null_patterns"]["patterns"].reverse()
    _write_yaml_file(target, data)

    assert "stats.null-patterns-not-ordered" in _codes(validate_print(print_dir))


def test_stats_null_patterns_duplicate_combination(print_dir: Path) -> None:
    target = print_dir / "seedbank/accession/statistics.yaml"
    data = _load_yaml_file(target)
    first = data["null_patterns"]["patterns"][0]
    data["null_patterns"]["patterns"].append({"columns": list(first["columns"]), "count": 0})
    _write_yaml_file(target, data)

    assert "stats.null-patterns-duplicate-combination" in _codes(validate_print(print_dir))


def test_stats_null_patterns_reconciliation_mismatch(print_dir: Path) -> None:
    """The cross-field identity: a column's own null_count moves, the census does not."""

    target = print_dir / "seedbank/accession/statistics.yaml"
    data = _load_yaml_file(target)
    column = data["null_patterns"]["patterns"][0]["columns"][0]
    data["columns"][column]["null_count"] += 1
    _write_yaml_file(target, data)

    assert "stats.null-patterns-reconciliation-mismatch" in _codes(validate_print(print_dir))


def test_stats_null_patterns_reconciliation_mismatch_under_bounded_coverage(
    print_dir: Path,
) -> None:
    target = print_dir / "seedbank/accession/statistics.yaml"
    data = _load_yaml_file(target)
    column = data["null_patterns"]["patterns"][0]["columns"][0]
    data["columns"][column]["null_count"] += 1
    data["null_patterns"]["coverage_method"] = "bounded"
    _write_yaml_file(target, data)
    codes = _codes(validate_print(print_dir))

    assert "stats.null-patterns-reconciliation-mismatch" not in codes
    assert "stats.null-patterns-reconciliation-mismatch-bounded" in codes


def test_stats_null_patterns_reconciliation_mismatch_with_no_method_field_is_unchanged(
    print_dir: Path,
) -> None:
    """An artifact carrying no method field at all is not `bounded` (SPEC 7.3's absence rule)."""

    target = print_dir / "seedbank/accession/statistics.yaml"
    data = _load_yaml_file(target)
    column = data["null_patterns"]["patterns"][0]["columns"][0]
    data["columns"][column]["null_count"] += 1
    data["null_patterns"].pop("coverage_method", None)  # simulate an artifact predating it
    _write_yaml_file(target, data)
    codes = _codes(validate_print(print_dir))

    assert "stats.null-patterns-reconciliation-mismatch-bounded" not in codes
    assert "stats.null-patterns-reconciliation-mismatch" in codes


def test_stats_physical_layout_unknown_column(print_dir: Path) -> None:
    target = print_dir / "seedbank/storage_reading/statistics.yaml"
    data = _load_yaml_file(target)
    data["physical_layout"]["keys"][0]["column"] = "not_a_column"
    _write_yaml_file(target, data)

    assert "stats.physical-layout-unknown-column" in _codes(validate_print(print_dir))


def test_stats_physical_layout_key_not_declared(print_dir: Path) -> None:
    """A column claims membership the table-level list does not name."""

    target = print_dir / "seedbank/storage_reading/statistics.yaml"
    data = _load_yaml_file(target)
    data["columns"]["temperature_c"]["physical_layout_key"] = True
    _write_yaml_file(target, data)

    assert "stats.physical-layout-key-not-declared" in _codes(validate_print(print_dir))


def test_stats_physical_layout_key_missing_marker(print_dir: Path) -> None:
    """The table-level list names a column that never carries the per-column marker."""

    target = print_dir / "seedbank/storage_reading/statistics.yaml"
    data = _load_yaml_file(target)
    del data["columns"]["reading_date"]["physical_layout_key"]
    _write_yaml_file(target, data)

    assert "stats.physical-layout-key-missing-marker" in _codes(validate_print(print_dir))


def test_stats_grain_unknown_column(print_dir: Path) -> None:
    target = print_dir / "seedbank/accession/statistics.yaml"
    data = _load_yaml_file(target)
    data["grain"]["keys"][0]["columns"] = ["not_a_column"]
    _write_yaml_file(target, data)

    assert "stats.grain-unknown-column" in _codes(validate_print(print_dir))


def test_stats_grain_duplicate_key(print_dir: Path) -> None:
    target = print_dir / "seedbank/accession/statistics.yaml"
    data = _load_yaml_file(target)
    data["grain"]["keys"].append(data["grain"]["keys"][0])
    _write_yaml_file(target, data)

    assert "stats.grain-duplicate-key" in _codes(validate_print(print_dir))


def test_stats_grain_duplicate_key_is_order_independent(print_dir: Path) -> None:
    """Key order is meaningful for encoding (SPEC 2.2.12), not for spotting a repeat."""

    target = print_dir / "seedbank/vault/statistics.yaml"
    data = _load_yaml_file(target)
    declared = next(k for k in data["grain"]["keys"] if k["detection"] == "declared")
    reordered = {"columns": list(reversed(declared["columns"])), "detection": "declared"}
    data["grain"]["keys"].append(reordered)
    _write_yaml_file(target, data)

    assert "stats.grain-duplicate-key" in _codes(validate_print(print_dir))


def test_stats_grain_absent_errors(print_dir: Path) -> None:
    """SPEC 2.2.12 requires the block: without it the file states nothing about its own grain."""

    target = print_dir / "seedbank/accession/statistics.yaml"
    data = _load_yaml_file(target)
    del data["grain"]
    _write_yaml_file(target, data)

    assert "schema.missing-required-field" in _codes(validate_print(print_dir))


def test_stats_grain_measured_under_scope(print_dir: Path) -> None:
    """`vault` carries a genuine measured entry; a `scope` block beside it overclaims."""

    target = print_dir / "seedbank/vault/statistics.yaml"
    data = _load_yaml_file(target)
    assert any(k["detection"] == "measured" for k in data["grain"]["keys"])
    data["scope"] = {"rows_scanned": 20, "sample": 0.5}
    _write_yaml_file(target, data)

    assert "stats.grain-measured-under-scope" in _codes(validate_print(print_dir))


def test_stats_grain_measured_on_empty_table(print_dir: Path) -> None:
    target = print_dir / "seedbank/vault/statistics.yaml"
    data = _load_yaml_file(target)
    assert any(k["detection"] == "measured" for k in data["grain"]["keys"])
    data["row_count"] = 0
    _write_yaml_file(target, data)

    assert "stats.grain-measured-on-empty-table" in _codes(validate_print(print_dir))


def test_stats_dependencies_unknown_column(print_dir: Path) -> None:
    target = print_dir / "seedbank/accession/statistics.yaml"
    data = _load_yaml_file(target)
    data["dependencies"] = [
        {"determinant": "not_a_column", "dependent": "accession_code", "strength": 1.0},
    ]
    _write_yaml_file(target, data)

    assert "stats.dependencies-unknown-column" in _codes(validate_print(print_dir))


def test_stats_dependencies_self_referential(print_dir: Path) -> None:
    target = print_dir / "seedbank/accession/statistics.yaml"
    data = _load_yaml_file(target)
    data["dependencies"] = [
        {"determinant": "accession_id", "dependent": "accession_id", "strength": 1.0},
    ]
    _write_yaml_file(target, data)

    assert "stats.dependencies-self-referential" in _codes(validate_print(print_dir))


def test_stats_dependencies_strength_out_of_range(print_dir: Path) -> None:
    target = print_dir / "seedbank/accession/statistics.yaml"
    data = _load_yaml_file(target)
    data["dependencies"] = [
        {"determinant": "accession_id", "dependent": "accession_code", "strength": 1.5},
    ]
    _write_yaml_file(target, data)

    assert "stats.dependencies-strength-out-of-range" in _codes(validate_print(print_dir))


def test_stats_dependencies_absent_is_conformant(print_dir: Path) -> None:
    """A MINOR-version addition (SPEC 5): its absence must not fail schema validation."""

    target = print_dir / "seedbank/accession/statistics.yaml"
    data = _load_yaml_file(target)
    data.pop("dependencies", None)
    _write_yaml_file(target, data)

    assert "schema.missing-required-field" not in _codes(validate_print(print_dir))


def test_stats_dependencies_direction_impossible(print_dir: Path) -> None:
    """A function's image cannot exceed its domain, independent of the strength claimed."""

    target = print_dir / "seedbank/accession/statistics.yaml"
    data = _load_yaml_file(target)
    data["columns"]["accession_id"]["cardinality"] = 5
    data["columns"]["accession_code"]["cardinality"] = 50
    data["dependencies"] = [
        {"determinant": "accession_id", "dependent": "accession_code", "strength": 1.0},
    ]
    _write_yaml_file(target, data)

    assert "stats.dependencies-direction-impossible" in _codes(validate_print(print_dir))


def test_stats_dependencies_measured_under_scope(print_dir: Path) -> None:
    target = print_dir / "seedbank/accession/statistics.yaml"
    data = _load_yaml_file(target)
    data["dependencies"] = [
        {"determinant": "accession_id", "dependent": "accession_code", "strength": 1.0},
    ]
    data["scope"] = {"rows_scanned": 20, "sample": 0.5}
    _write_yaml_file(target, data)

    assert "stats.dependencies-measured-under-scope" in _codes(validate_print(print_dir))


def test_stats_dependencies_measured_on_empty_table(print_dir: Path) -> None:
    target = print_dir / "seedbank/accession/statistics.yaml"
    data = _load_yaml_file(target)
    data["dependencies"] = [
        {"determinant": "accession_id", "dependent": "accession_code", "strength": 1.0},
    ]
    data["row_count"] = 0
    _write_yaml_file(target, data)

    assert "stats.dependencies-measured-on-empty-table" in _codes(validate_print(print_dir))


def test_stats_distribution_mismatch(print_dir: Path) -> None:
    """The mismatch check only verifies against an exhaustive `values` list (SPEC 2.2.5)."""

    target = print_dir / "seedbank/taxon/statistics.yaml"
    data = _load_yaml_file(target)
    # rank is exhaustive and currently 'dominant_value'; lie and say 'uniform'
    data["columns"]["rank"]["distribution"] = "uniform"
    _write_yaml_file(target, data)
    assert "stats.distribution-mismatch" in _codes(validate_print(print_dir))


def test_stats_sketch_unknown_method(print_dir: Path) -> None:
    target = print_dir / "seedbank/collector/statistics.yaml"
    data = _load_yaml_file(target)
    data["columns"]["collector_id"]["sketch"]["method"] = "kmv_sha256"
    _write_yaml_file(target, data)

    assert "stats.sketch-unknown-method" in _codes(validate_print(print_dir))


def test_stats_sketch_invalid_encoding(print_dir: Path) -> None:
    target = print_dir / "seedbank/collector/statistics.yaml"
    data = _load_yaml_file(target)
    data["columns"]["collector_id"]["sketch"]["values"] = "not valid base64!!!"
    _write_yaml_file(target, data)

    assert "stats.sketch-invalid-encoding" in _codes(validate_print(print_dir))


def test_stats_sketch_not_ascending(print_dir: Path) -> None:
    target = print_dir / "seedbank/collector/statistics.yaml"
    data = _load_yaml_file(target)
    encoded = data["columns"]["collector_id"]["sketch"]["values"]
    raw = base64.b64decode(encoded)
    reversed_raw = raw[-8:] + raw[8:-8] + raw[:8]  # swap first and last 8-byte hash
    data["columns"]["collector_id"]["sketch"]["values"] = base64.b64encode(
        reversed_raw,
    ).decode("ascii")
    _write_yaml_file(target, data)

    assert "stats.sketch-not-ascending" in _codes(validate_print(print_dir))


# --- Relationships invariants ---------------------------------------


def test_relationships_column_array_length_mismatch(print_dir: Path) -> None:
    target = print_dir / "seedbank/accession/relationships.yaml"
    data = _load_yaml_file(target)
    data["refers_to"][0]["column"] = ["a", "b"]
    data["refers_to"][0]["target_column"] = ["x"]
    _write_yaml_file(target, data)
    assert "relationships.column-array-length-mismatch" in _codes(validate_print(print_dir))


def test_relationships_broken_reciprocity(print_dir: Path) -> None:
    # germination_trial's collector_id already infers an edge (SPEC 2.3.8), so name another table.
    target = print_dir / "seedbank/collector/relationships.yaml"
    data = _load_yaml_file(target)
    data["referenced_by"].append(
        {
            "column": ["collector_id"],
            "referencer_table": "seedbank.germination_by_taxon_mv",
            "referencer_column": ["no_such_column_id"],
            "on_delete": "NO ACTION",
            "on_update": "NO ACTION",
            "detection": "declared",
        },
    )
    _write_yaml_file(target, data)
    assert "relationships.broken-reciprocity" in _codes(validate_print(print_dir))


def test_out_of_scope_referencer_does_not_break_reciprocity(print_dir: Path) -> None:
    """A referencer outside the print is invisible by design (SPEC 2.3.6), not broken.

    The self-reference pins that an absent manifest entry cannot bypass the check.
    """

    target = print_dir / "seedbank/collector/relationships.yaml"
    data = _load_yaml_file(target)

    # Self-reference: makes collector a target of its own refers_to.
    data["refers_to"].append(
        {
            "column": ["parent_id"],
            "target_table": "seedbank.collector",
            "target_column": ["collector_id"],
            "on_delete": "NO ACTION",
            "on_update": "NO ACTION",
            "detection": "declared",
        },
    )
    data["referenced_by"].append(
        {
            "column": ["collector_id"],
            "referencer_table": "fixture.staging.not_in_this_print",
            "referencer_column": ["collector_id"],
            "on_delete": "NO ACTION",
            "on_update": "NO ACTION",
            "detection": "declared",
        },
    )
    _write_yaml_file(target, data)

    assert "relationships.broken-reciprocity" not in _codes(validate_print(print_dir))


def test_relationships_ineligible_target_is_referenced(print_dir: Path) -> None:
    """SPEC 2.3.8: nothing can reference an object with no column to target."""

    target = print_dir / "seedbank/collector/relationships.yaml"
    data = _load_yaml_file(target)
    data["eligible_target"] = False
    _write_yaml_file(target, data)

    assert "relationships.ineligible-target-is-referenced" in _codes(validate_print(print_dir))


def test_an_eligible_target_with_referenced_by_is_unaffected(print_dir: Path) -> None:
    target = print_dir / "seedbank/collector/relationships.yaml"
    data = _load_yaml_file(target)
    data["eligible_target"] = True
    _write_yaml_file(target, data)

    assert "relationships.ineligible-target-is-referenced" not in _codes(validate_print(print_dir))


def test_an_ineligible_target_with_no_referenced_by_is_unaffected(print_dir: Path) -> None:
    target = print_dir / "seedbank/collector/relationships.yaml"
    data = _load_yaml_file(target)
    data["eligible_target"] = False
    data["referenced_by"] = []
    _write_yaml_file(target, data)

    assert "relationships.ineligible-target-is-referenced" not in _codes(validate_print(print_dir))


def test_relationships_observed_fanout_mismatch(print_dir: Path) -> None:
    """SPEC 2.3.10: `fanout_avg` must recompute to row_count/cardinality on the referencing side."""

    target = print_dir / "seedbank/accession/relationships.yaml"
    data = _load_yaml_file(target)
    data["refers_to"][0]["observed"]["fanout_avg"] = 999.0
    _write_yaml_file(target, data)

    assert "relationships.observed-fanout-mismatch" in _codes(validate_print(print_dir))


def test_relationships_observed_fanout_mismatch_at_the_rounding_boundary(print_dir: Path) -> None:
    """SPEC 2.2.6: six decimals, not one - `8.3` is wrong even though it looks close."""

    target = print_dir / "seedbank/accession/relationships.yaml"
    data = _load_yaml_file(target)
    assert data["refers_to"][1]["target_table"] == "seedbank.taxon"
    data["refers_to"][1]["observed"]["fanout_avg"] = 8.3
    _write_yaml_file(target, data)

    assert "relationships.observed-fanout-mismatch" in _codes(validate_print(print_dir))


def test_relationships_observed_coverage_mismatch(print_dir: Path) -> None:
    """SPEC 2.3.10: `target_coverage` must recompute to the two endpoints' own cardinality."""

    target = print_dir / "seedbank/accession/relationships.yaml"
    data = _load_yaml_file(target)
    data["refers_to"][0]["observed"]["target_coverage"] = 0.001
    _write_yaml_file(target, data)

    assert "relationships.observed-coverage-mismatch" in _codes(validate_print(print_dir))


def test_relationships_observed_containment_mismatch_on_a_declared_edge(print_dir: Path) -> None:
    """SPEC 2.2.14: an enforced edge recomputes to 1.0; a lower published value is caught."""

    target = print_dir / "seedbank/germination_trial/relationships.yaml"
    data = _load_yaml_file(target)
    assert data["refers_to"][0]["target_table"] == "seedbank.accession"
    assert data["refers_to"][0]["detection"] == "declared"
    data["refers_to"][0]["observed"]["containment"] = 0.9
    _write_yaml_file(target, data)

    assert "relationships.observed-containment-mismatch" in _codes(validate_print(print_dir))


def test_relationships_observed_answerable_count_mismatch(print_dir: Path) -> None:
    """SPEC 2.2.14/2.3.10: `answerable_count` must recompute to the two endpoints' sketches."""

    target = print_dir / "seedbank/germination_trial/relationships.yaml"
    data = _load_yaml_file(target)
    assert data["refers_to"][0]["target_table"] == "seedbank.accession"
    data["refers_to"][0]["observed"]["answerable_count"] += 1
    _write_yaml_file(target, data)

    codes = _codes(validate_print(print_dir))
    assert "relationships.observed-answerable-count-mismatch" in codes


def test_relationships_observed_coherent_mismatch(print_dir: Path) -> None:
    """SPEC 2.3.10: `coherent` must agree with the referencing/referenced cardinality comparison."""

    target = print_dir / "seedbank/accession/relationships.yaml"
    data = _load_yaml_file(target)
    data["refers_to"][0]["observed"]["coherent"] = False
    _write_yaml_file(target, data)

    assert "relationships.observed-coherent-mismatch" in _codes(validate_print(print_dir))


_BASE = {
    "format_version": 1,
    "table": "public.t",
    "profiled_at": "2026-01-01T00:00:00Z",
    "referenced_by": [],
}


def test_a_declared_edge_without_referential_actions_fails_the_schema() -> None:
    payload = {
        **_BASE,
        "refers_to": [
            {
                "column": ["a_id"],
                "target_table": "public.a",
                "target_column": ["id"],
                "detection": "declared",
            },
        ],
    }

    assert "schema.missing-required-field" in {i.code for i in check_relationships(payload, "t")}


def test_an_inferred_edge_without_referential_actions_conforms() -> None:
    payload = {
        **_BASE,
        "refers_to": [
            {
                "column": ["a_id"],
                "target_table": "public.a",
                "target_column": ["id"],
                "detection": "inferred",
            },
        ],
    }

    assert check_relationships(payload, "t") == []


def test_eligible_target_rejects_a_non_boolean() -> None:
    payload = {**_BASE, "eligible_target": "yes", "refers_to": []}

    assert "schema.type-mismatch" in {i.code for i in check_relationships(payload, "t")}


# --- Diff invariants ------------------------------------------------


def test_diff_summary_count_mismatch(print_dir: Path) -> None:
    target = print_dir / "diff.yaml"
    data = _load_yaml_file(target)
    data["summary"]["tables_added"] = 99
    _write_yaml_file(target, data)
    assert "diff.summary-count-mismatch" in _codes(validate_print(print_dir))


def test_diff_summary_total_mismatch(print_dir: Path) -> None:
    """An object absorbed into a counter it was never compared for (SPEC 2.6.4)."""

    target = print_dir / "diff.yaml"
    data = _load_yaml_file(target)
    data["summary"]["unevaluated_tables"] += 1
    _write_yaml_file(target, data)

    assert "diff.summary-total-mismatch" in _codes(validate_print(print_dir))


def test_diff_summary_totals_that_add_up_are_not_reported(print_dir: Path) -> None:
    """The control: the reference example's own counters partition its scanned set."""

    assert "diff.summary-total-mismatch" not in _codes(validate_print(print_dir))


def test_diff_relationship_modified_no_change(print_dir: Path) -> None:
    target = print_dir / "diff.yaml"
    data = _load_yaml_file(target)
    data["changes"].append(
        {
            "kind": "relationship_modified",
            "source_table": "a",
            "source_column": ["x"],
            "target_table": "b",
            "target_column": ["y"],
        },
    )
    _write_yaml_file(target, data)
    assert "diff.relationship-modified-no-change" in _codes(validate_print(print_dir))


def test_diff_comment_target_column_mismatch(print_dir: Path) -> None:
    target = print_dir / "diff.yaml"
    data = _load_yaml_file(target)
    data["changes"].append(
        {
            "kind": "comment_changed",
            "table": "a",
            "target": "column",
            "before": None,
            "after": "foo",
        },
    )
    _write_yaml_file(target, data)
    assert "diff.comment-target-column-mismatch" in _codes(validate_print(print_dir))


def test_diff_statistic_changed_delta_on_non_numeric(print_dir: Path) -> None:
    target = print_dir / "diff.yaml"
    data = _load_yaml_file(target)
    data["changes"].append(
        {
            "kind": "statistic_changed",
            "table": "a",
            "column": "x",
            "stat": "distribution",
            "before": "uniform",
            "after": "imbalanced",
            "delta": 1,
        },
    )
    _write_yaml_file(target, data)
    assert "diff.statistic-changed-delta-on-non-numeric" in _codes(validate_print(print_dir))


def test_diff_statistic_changed_delta_pct_sign_mismatch(print_dir: Path) -> None:
    target = print_dir / "diff.yaml"
    data = _load_yaml_file(target)
    data["changes"].append(
        {
            "kind": "statistic_changed",
            "table": "a",
            "column": "x",
            "stat": "cardinality",
            "before": -100,
            "after": -110,
            "delta": -10,
            "delta_pct": 0.1,
        },
    )
    _write_yaml_file(target, data)
    codes = _codes(validate_print(print_dir))
    assert "diff.statistic-changed-delta-pct-sign-mismatch" in codes


def test_diff_row_count_changed_delta_mismatch(print_dir: Path) -> None:
    target = print_dir / "diff.yaml"
    data = _load_yaml_file(target)
    data["changes"].append(
        {
            "kind": "table_row_count_changed",
            "table": "a",
            "before": 100,
            "after": 120,
            "delta": 5,
            "before_method": "exact",
            "after_method": "exact",
        },
    )
    _write_yaml_file(target, data)
    codes = _codes(validate_print(print_dir))
    assert "diff.row-count-changed-delta-mismatch" in codes


def test_diff_grain_changed_no_change(print_dir: Path) -> None:
    target = print_dir / "diff.yaml"
    data = _load_yaml_file(target)
    block = {"keys": [{"columns": ["id"], "detection": "declared"}]}
    data["changes"].append(
        {"kind": "grain_changed", "table": "a", "before": block, "after": block},
    )
    _write_yaml_file(target, data)
    codes = _codes(validate_print(print_dir))
    assert "diff.grain-changed-no-change" in codes


def test_diff_physical_layout_changed_no_change(print_dir: Path) -> None:
    target = print_dir / "diff.yaml"
    data = _load_yaml_file(target)
    block = {"mechanism": "cluster", "keys": [{"expression": "id"}]}
    data["changes"].append(
        {"kind": "physical_layout_changed", "table": "a", "before": block, "after": block},
    )
    _write_yaml_file(target, data)
    codes = _codes(validate_print(print_dir))
    assert "diff.physical-layout-changed-no-change" in codes


# --- DDL ------------------------------------------------------------


def test_ddl_empty_file(print_dir: Path) -> None:
    (print_dir / "seedbank/accession/ddl.sql").write_text("")
    assert "ddl.empty-file" in _codes(validate_print(print_dir))


def test_ddl_missing_trailing_newline(print_dir: Path) -> None:
    target = print_dir / "seedbank/accession/ddl.sql"
    content = target.read_text().rstrip("\n")
    target.write_text(content)
    assert "ddl.missing-trailing-newline" in _codes(validate_print(print_dir))


# --- Scope (SPEC 2.2.8) ---------------------------------------------


_STATS = "seedbank/accession/statistics.yaml"


def _with_scope(print_dir: Path, scope: Any, row_count_method: Any = None) -> list[Issue]:
    """Write `scope` and optional `row_count_method` into the stats artifact, then validate."""

    target = print_dir / _STATS
    data = _load_yaml_file(target)
    data["scope"] = scope

    if row_count_method is not None:
        data["row_count_method"] = row_count_method
    _write_yaml_file(target, data)

    return validate_print(print_dir)


def test_scope_absent_is_conformant(print_dir: Path) -> None:
    """Absence is the assertion that nothing was skipped."""

    codes = _codes(validate_print(print_dir))

    assert not {c for c in codes if c.startswith("stats.scope-")}


def test_scope_not_a_mapping(print_dir: Path) -> None:
    assert "stats.scope-not-a-mapping" in _codes(_with_scope(print_dir, "half of it"))


def test_scope_missing_rows_scanned(print_dir: Path) -> None:
    assert "stats.scope-missing-rows-scanned" in _codes(_with_scope(print_dir, {"sample": 0.5}))


def test_scope_rows_scanned_exceeds_row_count(print_dir: Path) -> None:
    """A count claiming to be exact cannot be smaller than a subset of itself."""

    codes = _codes(_with_scope(print_dir, {"rows_scanned": 10_000_000}, row_count_method="exact"))

    assert "stats.scope-rows-scanned-exceeds-row-count" in codes


def test_scope_rows_scanned_may_exceed_an_estimated_row_count(print_dir: Path) -> None:
    """SPEC 2.2.8's exception: an estimate that undershot is not a violation."""

    row_count = _load_yaml_file(print_dir / _STATS)["row_count"]
    codes = _codes(
        _with_scope(
            print_dir,
            {"rows_scanned": row_count + 1, "sample": 0.5},
            row_count_method="approximate",
        ),
    )

    assert "stats.scope-rows-scanned-exceeds-row-count" not in codes


def test_scope_rows_scanned_exceeds_a_row_count_of_unstated_method(print_dir: Path) -> None:
    """Only an explicit `approximate` buys the exception; silence is not a claim."""

    target = print_dir / _STATS
    data = _load_yaml_file(target)
    del data["row_count_method"]
    _write_yaml_file(target, data)

    codes = _codes(_with_scope(print_dir, {"rows_scanned": 10_000_000}))

    assert "stats.scope-rows-scanned-exceeds-row-count" in codes


@pytest.mark.parametrize(
    "sample",
    [0, -0.1, 1.5, "half"],
    ids=["zero", "negative", "above-one", "text"],
)
def test_scope_sample_out_of_range(print_dir: Path, sample: Any) -> None:
    codes = _codes(_with_scope(print_dir, {"rows_scanned": 1, "sample": sample}))

    assert "stats.scope-sample-out-of-range" in codes


def test_scope_sample_and_filter(print_dir: Path) -> None:
    """A block naming two narrowings describes a read no producer performs."""

    scope = {"rows_scanned": 1, "sample": 0.5, "filter": "id IS NOT NULL"}

    assert "stats.scope-sample-and-filter" in _codes(_with_scope(print_dir, scope))


def test_stats_excess_precision(print_dir: Path) -> None:
    """SPEC 2.2.6 rounds to six decimal places; a seventh is a producer that ignored it."""

    target = print_dir / _STATS
    data = _load_yaml_file(target)
    column = next(iter(data["columns"].values()))
    column["null_rate"] = 0.0123456789

    _write_yaml_file(target, data)
    issues = validate_print(print_dir)
    match = [i for i in issues if i.code == "stats.excess-precision"]

    assert match, _codes(issues)
    assert match[0].severity == "error"


def test_six_decimal_places_is_accepted(print_dir: Path) -> None:
    """The boundary is inclusive - exactly six places is what the rule asks for."""

    target = print_dir / _STATS
    data = _load_yaml_file(target)
    column = next(iter(data["columns"].values()))
    column["null_rate"] = 0.012345

    _write_yaml_file(target, data)

    assert "stats.excess-precision" not in _codes(validate_print(print_dir))


def test_privacy_unredacted_sensitive(print_dir: Path) -> None:
    """SPEC 4.4.2: a named category publishing a cell value with no marker warns."""

    target = print_dir / _STATS
    data = _load_yaml_file(target)
    column = next(
        c for c in data["columns"].values() if "values" in c or "range" in c or "percentiles" in c
    )
    column.setdefault("inferred", {})["sensitivity"] = "contact"
    column.pop("redacted", None)

    _write_yaml_file(target, data)
    issues = validate_print(print_dir)
    match = [i for i in issues if i.code == "privacy.unredacted-sensitive"]

    assert match, _codes(issues)
    assert match[0].severity == "warning"
    assert not any(i.severity == "error" for i in match)


def test_scope_that_asserts_nothing_warns(print_dir: Path) -> None:
    """A block covering the whole table records nothing worth reading."""

    target = print_dir / _STATS
    row_count = _load_yaml_file(target)["row_count"]
    issues = _with_scope(print_dir, {"rows_scanned": row_count})
    match = [i for i in issues if i.code == "stats.scope-asserts-nothing"]

    assert match, _codes(issues)
    assert match[0].severity == "warning"


@pytest.mark.parametrize(
    "narrowing",
    [{"sample": 0.5}, {"filter": "collected_on >= '2026-01-01'"}],
    ids=["sample", "filter"],
)
def test_a_real_subset_is_conformant(print_dir: Path, narrowing: dict[str, Any]) -> None:
    """The two shapes the block exists to admit."""

    target = print_dir / _STATS
    data = _load_yaml_file(target)
    row_count = data["row_count"]
    scanned = row_count // 2
    data["scope"] = {"rows_scanned": scanned, **narrowing}

    # Ratios are relative to the scanned set (SPEC 2.2.8); restate counts against it.
    for col in data["columns"].values():
        if isinstance(col.get("null_count"), int):
            col["null_count"] = 0

        if isinstance(col.get("cardinality"), int):
            col["cardinality"] = min(col["cardinality"], scanned)

        if isinstance(col.get("values"), list):
            col["values"] = [{"value": "only", "count": scanned}]
            col["values_coverage"] = 1.0

    _write_yaml_file(target, data)
    codes = _codes(validate_print(print_dir))

    assert not {c for c in codes if c.startswith("stats.scope-")}, codes


@pytest.mark.parametrize(
    "narrowing",
    [{"sample": 0.5}, {"filter": "collected_on >= '2026-01-01'"}],
    ids=["sample", "filter"],
)
def test_a_real_subset_without_the_population_marker_is_flagged(
    print_dir: Path,
    narrowing: dict[str, Any],
) -> None:
    """The control for the next test: omitting the marker on a scoped file is an error."""

    target = print_dir / _STATS
    data = _load_yaml_file(target)
    data["scope"] = {"rows_scanned": data["row_count"] // 2, **narrowing}
    _write_yaml_file(target, data)

    assert "stats.population-marker-mismatch" in _codes(validate_print(print_dir))


@pytest.mark.parametrize(
    "narrowing",
    [{"sample": 0.5}, {"filter": "collected_on >= '2026-01-01'"}],
    ids=["sample", "filter"],
)
def test_a_real_subset_with_the_population_marker_on_every_column_is_conformant(
    print_dir: Path,
    narrowing: dict[str, Any],
) -> None:
    target = print_dir / _STATS
    data = _load_yaml_file(target)
    row_count = data["row_count"]
    scanned = row_count // 2
    data["scope"] = {"rows_scanned": scanned, **narrowing}

    # Ratios are relative to the scanned set (SPEC 2.2.8); restate counts against it.
    for col in data["columns"].values():
        col["rows_scanned"] = scanned

        if isinstance(col.get("null_count"), int):
            col["null_count"] = 0

        if isinstance(col.get("cardinality"), int):
            col["cardinality"] = min(col["cardinality"], scanned)

        if isinstance(col.get("values"), list):
            col["values"] = [{"value": "only", "count": scanned}]
            col["values_coverage"] = 1.0

    _write_yaml_file(target, data)

    assert "stats.population-marker-mismatch" not in _codes(validate_print(print_dir))


# --- Annotations (SPEC 2.7.1) -----------------------------------------

_ANNOTATIONS = "seedbank/accession/statistics.annotations.yaml"


def test_annotations_registered_no_unknown_file_warning(print_dir: Path) -> None:
    """The reference example already ships one; this pins that it stays registered."""

    assert (print_dir / _ANNOTATIONS).is_file()
    assert "layout.unknown-file" not in _codes(validate_print(print_dir))


def test_annotations_unknown_column(print_dir: Path) -> None:
    target = print_dir / _ANNOTATIONS
    data = _load_yaml_file(target)
    data["columns"]["not_a_real_column"] = {"note": "stale key"}
    _write_yaml_file(target, data)

    issues = validate_print(print_dir)
    match = [i for i in issues if i.code == "annotations.unknown-column"]

    assert match, _codes(issues)
    assert match[0].severity == "warning"


def test_annotations_stale_key_does_not_fail_the_run(print_dir: Path) -> None:
    target = print_dir / _ANNOTATIONS
    data = _load_yaml_file(target)
    data["columns"]["not_a_real_column"] = {"note": "stale key"}
    _write_yaml_file(target, data)

    assert [i for i in validate_print(print_dir) if i.severity == "error"] == []


def test_annotations_stale_key_check_reaches_a_view(print_dir: Path) -> None:
    """A stale annotation key is checkable against a view's catalog-only columns (SPEC 2.2.15)."""

    target = print_dir / "seedbank/accession_summary/statistics.annotations.yaml"
    data = _load_yaml_file(target)
    data["columns"]["not_a_real_column"] = {"note": "stale key"}
    _write_yaml_file(target, data)

    assert "annotations.unknown-column" in _codes(validate_print(print_dir))


def test_annotations_missing_format_version(print_dir: Path) -> None:
    target = print_dir / _ANNOTATIONS
    data = _load_yaml_file(target)
    del data["format_version"]
    _write_yaml_file(target, data)

    assert "version.missing-format-version" in _codes(validate_print(print_dir))


def test_annotations_claim_contradicts_statistic(print_dir: Path) -> None:
    """`provenance_country` publishes `null_rate: 0.0`; a claim of 0.5 is wrong."""

    target = print_dir / _ANNOTATIONS
    data = _load_yaml_file(target)
    data["columns"]["taxon_id"] = {"claims": {"null_rate": 0.5}}
    _write_yaml_file(target, data)

    issues = validate_print(print_dir)
    match = [i for i in issues if i.code == "annotations.claim-contradicts-statistic"]

    assert match, _codes(issues)
    assert match[0].severity == "warning"


def test_annotations_claim_matching_the_statistic_is_silent(print_dir: Path) -> None:
    target = print_dir / _ANNOTATIONS
    data = _load_yaml_file(target)
    data["columns"]["taxon_id"] = {"claims": {"classification": "foreign_key_candidate"}}
    _write_yaml_file(target, data)

    codes = _codes(validate_print(print_dir))

    assert "annotations.claim-contradicts-statistic" not in codes
    assert "annotations.claim-unassertable" not in codes


def test_annotations_claim_unknown_stat_is_unassertable(print_dir: Path) -> None:
    target = print_dir / _ANNOTATIONS
    data = _load_yaml_file(target)
    data["columns"]["taxon_id"] = {"claims": {"not_a_real_stat": 1}}
    _write_yaml_file(target, data)

    issues = validate_print(print_dir)
    match = [i for i in issues if i.code == "annotations.claim-unassertable"]

    assert match, _codes(issues)
    assert match[0].severity == "warning"


def test_annotations_claim_malformed_predicate_is_unassertable(print_dir: Path) -> None:
    target = print_dir / _ANNOTATIONS
    data = _load_yaml_file(target)
    # `range` accepts only `min`/`max` keys; this predicate shape is unrecognized.
    data["columns"]["taxon_id"] = {"claims": {"range.min": {"eq": 1}}}
    _write_yaml_file(target, data)

    assert "annotations.claim-unassertable" in _codes(validate_print(print_dir))


def test_annotations_note_only_entry_has_no_claims_to_check(print_dir: Path) -> None:
    target = print_dir / _ANNOTATIONS
    data = _load_yaml_file(target)
    data["columns"]["taxon_id"] = {"note": "just prose, nothing checkable here"}
    _write_yaml_file(target, data)

    codes = _codes(validate_print(print_dir))

    assert "annotations.claim-contradicts-statistic" not in codes
    assert "annotations.claim-unassertable" not in codes


def test_annotations_claims_never_gate_conformance(print_dir: Path) -> None:
    target = print_dir / _ANNOTATIONS
    data = _load_yaml_file(target)
    data["columns"]["taxon_id"] = {"claims": {"null_rate": 0.5, "not_a_real_stat": 1}}
    _write_yaml_file(target, data)

    assert [i for i in validate_print(print_dir) if i.severity == "error"] == []


def test_annotations_root_closed_rejects_an_unknown_key(print_dir: Path) -> None:
    target = print_dir / _ANNOTATIONS
    data = _load_yaml_file(target)
    data["wibble"] = 123
    _write_yaml_file(target, data)

    assert "schema.type-mismatch" in _codes(validate_print(print_dir))


def test_annotations_grain_states_a_human_authored_key(print_dir: Path) -> None:
    """A grain key naming columns the table's statistics.yaml does have is silent."""

    target = print_dir / _ANNOTATIONS
    data = _load_yaml_file(target)
    data["grain"] = {"keys": [{"columns": ["taxon_id", "collector_id"], "note": "observed"}]}
    _write_yaml_file(target, data)

    codes = _codes(validate_print(print_dir))

    assert "annotations.grain-unknown-column" not in codes
    assert [i for i in validate_print(print_dir) if i.severity == "error"] == []


def test_annotations_grain_unknown_column(print_dir: Path) -> None:
    target = print_dir / _ANNOTATIONS
    data = _load_yaml_file(target)
    data["grain"] = {"keys": [{"columns": ["not_a_real_column"]}]}
    _write_yaml_file(target, data)

    issues = validate_print(print_dir)
    match = [i for i in issues if i.code == "annotations.grain-unknown-column"]

    assert match, _codes(issues)
    assert match[0].severity == "warning"


def test_annotations_grain_partially_unknown_column_still_flags_the_key(
    print_dir: Path,
) -> None:
    """One real column alongside a fake one still invalidates the whole key."""

    target = print_dir / _ANNOTATIONS
    data = _load_yaml_file(target)
    data["grain"] = {"keys": [{"columns": ["taxon_id", "not_a_real_column"]}]}
    _write_yaml_file(target, data)

    assert "annotations.grain-unknown-column" in _codes(validate_print(print_dir))


def test_annotations_grain_never_gates_conformance(print_dir: Path) -> None:
    target = print_dir / _ANNOTATIONS
    data = _load_yaml_file(target)
    data["grain"] = {"keys": [{"columns": ["not_a_real_column"]}]}
    _write_yaml_file(target, data)

    assert [i for i in validate_print(print_dir) if i.severity == "error"] == []


# --- Relationship annotations (SPEC 2.7.2) ---------------------------

_REL_ANNOTATIONS = "seedbank/germination_trial/relationships.annotations.yaml"


def _declare_relationship_annotations(print_dir: Path) -> None:
    """Add the manifest entry `_REL_ANNOTATIONS` needs before a test writes to it.

    The reference example declares no relationships.annotations.yaml, so no content-level
    check sees the file until its artifact key exists.
    """

    manifest_path = print_dir / "manifest.yaml"
    manifest = _load_yaml_file(manifest_path)
    manifest["tables"]["seedbank.germination_trial"]["artifacts"]["relationships_annotations"] = (
        "relationships.annotations.yaml"
    )
    _write_yaml_file(manifest_path, manifest)


def test_relationship_annotations_registered_no_unknown_file_warning(print_dir: Path) -> None:
    _declare_relationship_annotations(print_dir)
    target = print_dir / _REL_ANNOTATIONS
    _write_yaml_file(target, {"format_version": 1, "refers_to": []})

    assert "layout.unknown-file" not in _codes(validate_print(print_dir))


def test_relationship_annotations_verdict_on_inferred_edge_is_silent(print_dir: Path) -> None:
    """`germination_trial.collector_id -> collector.collector_id` is the inferred edge."""

    _declare_relationship_annotations(print_dir)
    target = print_dir / _REL_ANNOTATIONS
    _write_yaml_file(
        target,
        {
            "format_version": 1,
            "refers_to": [
                {
                    "column": ["collector_id"],
                    "target_table": "seedbank.collector",
                    "target_column": ["collector_id"],
                    "verdict": "rejected",
                    "note": "name coincidence, not a real FK",
                },
            ],
        },
    )

    codes = _codes(validate_print(print_dir))

    assert "annotations.unknown-edge" not in codes
    assert "annotations.verdict-on-declared-edge" not in codes


def test_relationship_annotations_verdict_on_declared_edge_is_reported(print_dir: Path) -> None:
    """`germination_trial.accession_id -> accession.accession_id` is declared."""

    _declare_relationship_annotations(print_dir)
    target = print_dir / _REL_ANNOTATIONS
    _write_yaml_file(
        target,
        {
            "format_version": 1,
            "refers_to": [
                {
                    "column": ["accession_id"],
                    "target_table": "seedbank.accession",
                    "target_column": ["accession_id"],
                    "verdict": "rejected",
                },
            ],
        },
    )

    issues = validate_print(print_dir)
    match = [i for i in issues if i.code == "annotations.verdict-on-declared-edge"]

    assert match, _codes(issues)
    assert match[0].severity == "warning"


def test_relationship_annotations_verdict_on_unknown_edge_is_stale(print_dir: Path) -> None:
    _declare_relationship_annotations(print_dir)
    target = print_dir / _REL_ANNOTATIONS
    _write_yaml_file(
        target,
        {
            "format_version": 1,
            "refers_to": [
                {
                    "column": ["collector_id"],
                    "target_table": "seedbank.nonexistent",
                    "target_column": ["id"],
                    "verdict": "rejected",
                },
            ],
        },
    )

    issues = validate_print(print_dir)
    match = [i for i in issues if i.code == "annotations.unknown-edge"]

    assert match, _codes(issues)
    assert match[0].severity == "warning"


def test_relationship_annotations_addition_with_no_verdict_is_never_stale(print_dir: Path) -> None:
    """An entry with no verdict is a human addition; it needs no counterpart to resolve."""

    _declare_relationship_annotations(print_dir)
    target = print_dir / _REL_ANNOTATIONS
    _write_yaml_file(
        target,
        {
            "format_version": 1,
            "refers_to": [
                {
                    "column": ["collector_id"],
                    "target_table": "seedbank.nonexistent",
                    "target_column": ["id"],
                    "note": "not addressed at anything relationships.yaml emits",
                },
            ],
        },
    )

    codes = _codes(validate_print(print_dir))

    assert "annotations.unknown-edge" not in codes


def test_relationship_annotations_missing_format_version(print_dir: Path) -> None:
    _declare_relationship_annotations(print_dir)
    target = print_dir / _REL_ANNOTATIONS
    _write_yaml_file(target, {"refers_to": []})

    assert "version.missing-format-version" in _codes(validate_print(print_dir))


def test_relationship_annotations_never_gates_conformance(print_dir: Path) -> None:
    _declare_relationship_annotations(print_dir)
    target = print_dir / _REL_ANNOTATIONS
    _write_yaml_file(
        target,
        {
            "format_version": 1,
            "refers_to": [
                {
                    "column": ["accession_id"],
                    "target_table": "seedbank.accession",
                    "target_column": ["accession_id"],
                    "verdict": "rejected",
                },
            ],
        },
    )

    assert [i for i in validate_print(print_dir) if i.severity == "error"] == []


def test_relationship_annotations_root_closed_rejects_an_unknown_key(print_dir: Path) -> None:
    _declare_relationship_annotations(print_dir)
    target = print_dir / _REL_ANNOTATIONS
    _write_yaml_file(target, {"format_version": 1, "refers_to": [], "wibble": 123})

    assert "schema.type-mismatch" in _codes(validate_print(print_dir))


def test_relationship_annotations_claim_contradicts_observed(print_dir: Path) -> None:
    """`collector_id -> collector`'s `observed.fanout_max` publishes 3; a claim of 1 is wrong."""

    _declare_relationship_annotations(print_dir)
    target = print_dir / _REL_ANNOTATIONS
    _write_yaml_file(
        target,
        {
            "format_version": 1,
            "refers_to": [
                {
                    "column": ["collector_id"],
                    "target_table": "seedbank.collector",
                    "target_column": ["collector_id"],
                    "claims": {"observed.fanout_max": {"max": 1}},
                },
            ],
        },
    )

    issues = validate_print(print_dir)
    match = [i for i in issues if i.code == "annotations.claim-contradicts-statistic"]

    assert match, _codes(issues)
    assert match[0].severity == "warning"


def test_relationship_annotations_claim_matching_observed_is_silent(print_dir: Path) -> None:
    _declare_relationship_annotations(print_dir)
    target = print_dir / _REL_ANNOTATIONS
    _write_yaml_file(
        target,
        {
            "format_version": 1,
            "refers_to": [
                {
                    "column": ["collector_id"],
                    "target_table": "seedbank.collector",
                    "target_column": ["collector_id"],
                    "claims": {"observed.fanout_max": {"max": 5}},
                },
            ],
        },
    )

    codes = _codes(validate_print(print_dir))

    assert "annotations.claim-contradicts-statistic" not in codes
    assert "annotations.claim-unassertable" not in codes


def test_relationship_annotations_claim_contradicts_answerable_count(print_dir: Path) -> None:
    """`collector_id -> collector`'s `observed.answerable_count` publishes 400; a claim of 1 is wrong."""

    _declare_relationship_annotations(print_dir)
    target = print_dir / _REL_ANNOTATIONS
    _write_yaml_file(
        target,
        {
            "format_version": 1,
            "refers_to": [
                {
                    "column": ["collector_id"],
                    "target_table": "seedbank.collector",
                    "target_column": ["collector_id"],
                    "claims": {"observed.answerable_count": {"max": 1}},
                },
            ],
        },
    )

    issues = validate_print(print_dir)
    match = [i for i in issues if i.code == "annotations.claim-contradicts-statistic"]

    assert match, _codes(issues)
    assert match[0].severity == "warning"


def test_relationship_annotations_claim_matching_answerable_count_is_silent(
    print_dir: Path,
) -> None:
    _declare_relationship_annotations(print_dir)
    target = print_dir / _REL_ANNOTATIONS
    _write_yaml_file(
        target,
        {
            "format_version": 1,
            "refers_to": [
                {
                    "column": ["collector_id"],
                    "target_table": "seedbank.collector",
                    "target_column": ["collector_id"],
                    "claims": {"observed.answerable_count": {"min": 400}},
                },
            ],
        },
    )

    codes = _codes(validate_print(print_dir))

    assert "annotations.claim-contradicts-statistic" not in codes
    assert "annotations.claim-unassertable" not in codes


def test_relationship_annotations_claim_unknown_stat_is_unassertable(print_dir: Path) -> None:
    _declare_relationship_annotations(print_dir)
    target = print_dir / _REL_ANNOTATIONS
    _write_yaml_file(
        target,
        {
            "format_version": 1,
            "refers_to": [
                {
                    "column": ["collector_id"],
                    "target_table": "seedbank.collector",
                    "target_column": ["collector_id"],
                    "claims": {"null_rate": 0.0},
                },
            ],
        },
    )

    issues = validate_print(print_dir)
    match = [i for i in issues if i.code == "annotations.claim-unassertable"]

    assert match, _codes(issues)
    assert match[0].severity == "warning"


def test_relationship_annotations_claim_on_unknown_edge_is_unassertable(
    print_dir: Path,
) -> None:
    """No `verdict`, so `check_verdicts` stays silent (SPEC 2.7.2); nothing to resolve."""

    _declare_relationship_annotations(print_dir)
    target = print_dir / _REL_ANNOTATIONS
    _write_yaml_file(
        target,
        {
            "format_version": 1,
            "refers_to": [
                {
                    "column": ["collector_id"],
                    "target_table": "seedbank.nonexistent",
                    "target_column": ["id"],
                    "claims": {"observed.fanout_max": {"max": 1}},
                },
            ],
        },
    )

    codes = _codes(validate_print(print_dir))

    assert "annotations.claim-contradicts-statistic" not in codes
    assert "annotations.claim-unassertable" in codes
    assert "annotations.unknown-edge" not in codes


def test_relationship_annotations_claims_never_gate_conformance(print_dir: Path) -> None:
    _declare_relationship_annotations(print_dir)
    target = print_dir / _REL_ANNOTATIONS
    _write_yaml_file(
        target,
        {
            "format_version": 1,
            "refers_to": [
                {
                    "column": ["collector_id"],
                    "target_table": "seedbank.collector",
                    "target_column": ["collector_id"],
                    "claims": {"observed.fanout_max": {"max": 1}, "null_rate": 0.0},
                },
            ],
        },
    )

    assert [i for i in validate_print(print_dir) if i.severity == "error"] == []


# --- Path-valued relationship endpoints (SPEC 2.3.9) -------------------


def test_path_endpoint_on_a_single_column_edge_validates(print_dir: Path) -> None:
    target = print_dir / "seedbank/accession/relationships.yaml"
    data = _load_yaml_file(target)
    data["refers_to"][0]["path"] = ["id"]
    _write_yaml_file(target, data)

    codes = _codes(validate_print(print_dir))

    assert "relationships.path-on-composite-endpoint" not in codes
    assert "schema.type-mismatch" not in codes


def test_path_on_a_composite_column_endpoint_is_rejected(print_dir: Path) -> None:
    """`accession.refers_to` targets `vault` via the composite (vault_id, shelf_code) pair."""

    target = print_dir / "seedbank/accession/relationships.yaml"
    data = _load_yaml_file(target)
    composite = next(e for e in data["refers_to"] if len(e["column"]) > 1)
    composite["path"] = ["id"]
    _write_yaml_file(target, data)

    issues = validate_print(print_dir)
    match = [i for i in issues if i.code == "relationships.path-on-composite-endpoint"]

    assert match, _codes(issues)
    assert match[0].severity == "error"


def test_target_path_on_a_composite_target_column_is_rejected(print_dir: Path) -> None:
    target = print_dir / "seedbank/accession/relationships.yaml"
    data = _load_yaml_file(target)
    composite = next(e for e in data["refers_to"] if len(e["target_column"]) > 1)
    composite["target_path"] = ["id"]
    _write_yaml_file(target, data)

    codes = _codes(validate_print(print_dir))

    assert "relationships.path-on-composite-endpoint" in codes


def test_path_endpoint_authored_via_relationship_annotations_validates(print_dir: Path) -> None:
    """SPEC 2.7.2: the authoring channel for an edge the producer never emits."""

    _declare_relationship_annotations(print_dir)
    target = print_dir / _REL_ANNOTATIONS
    _write_yaml_file(
        target,
        {
            "format_version": 1,
            "refers_to": [
                {
                    "column": ["notes"],
                    "path": ["collector_email"],
                    "target_table": "seedbank.collector",
                    "target_column": ["institution_email"],
                    "note": "join key lives inside the notes JSON payload",
                },
            ],
        },
    )

    assert [i for i in validate_print(print_dir) if i.severity == "error"] == []


def test_composite_path_authored_via_relationship_annotations_is_rejected(print_dir: Path) -> None:
    _declare_relationship_annotations(print_dir)
    target = print_dir / _REL_ANNOTATIONS
    _write_yaml_file(
        target,
        {
            "format_version": 1,
            "refers_to": [
                {
                    "column": ["a", "b"],
                    "path": ["id"],
                    "target_table": "seedbank.collector",
                    "target_column": ["collector_id"],
                },
            ],
        },
    )

    codes = _codes(validate_print(print_dir))

    assert "relationships.path-on-composite-endpoint" in codes


# --- Value-grain annotations (SPEC 2.7.1) -------------------------------

_VAULT_ID_VALUES = "seedbank/accession/statistics.yaml"


def test_value_note_on_a_published_value_is_silent(print_dir: Path) -> None:
    """`accession.vault_id` publishes an exhaustive 1-8 domain."""

    target = print_dir / _ANNOTATIONS
    data = _load_yaml_file(target)
    data["columns"]["vault_id"] = {"values": [{"value": 3, "note": "the north wing shelf bank"}]}
    _write_yaml_file(target, data)

    codes = _codes(validate_print(print_dir))

    assert "annotations.unknown-value" not in codes


def test_value_note_on_an_unpublished_value_is_stale(print_dir: Path) -> None:
    target = print_dir / _ANNOTATIONS
    data = _load_yaml_file(target)
    data["columns"]["vault_id"] = {"values": [{"value": 99, "note": "does not exist"}]}
    _write_yaml_file(target, data)

    issues = validate_print(print_dir)
    match = [i for i in issues if i.code == "annotations.unknown-value"]

    assert match, _codes(issues)
    assert match[0].severity == "warning"


def test_value_note_under_a_truncated_list_is_never_stale(print_dir: Path) -> None:
    """A value may occur unlisted under a truncated (non-exhaustive) list - an addition."""

    stats_target = print_dir / _VAULT_ID_VALUES
    stats = _load_yaml_file(stats_target)
    stats["columns"]["vault_id"]["values_coverage"] = 0.5
    _write_yaml_file(stats_target, stats)

    ann_target = print_dir / _ANNOTATIONS
    ann = _load_yaml_file(ann_target)
    ann["columns"]["vault_id"] = {"values": [{"value": 99, "note": "not in the truncated list"}]}
    _write_yaml_file(ann_target, ann)

    assert "annotations.unknown-value" not in _codes(validate_print(print_dir))


def test_value_note_on_a_redacted_column_is_unassertable(print_dir: Path) -> None:
    stats_target = print_dir / _VAULT_ID_VALUES
    stats = _load_yaml_file(stats_target)
    stats["columns"]["vault_id"]["redacted"] = "mask"
    _write_yaml_file(stats_target, stats)

    ann_target = print_dir / _ANNOTATIONS
    ann = _load_yaml_file(ann_target)
    ann["columns"]["vault_id"] = {"values": [{"value": 3, "note": "the north wing shelf bank"}]}
    _write_yaml_file(ann_target, ann)

    issues = validate_print(print_dir)
    match = [i for i in issues if i.code == "annotations.value-note-unassertable"]

    assert match, _codes(issues)
    assert match[0].severity == "warning"


def test_value_notes_never_gate_conformance(print_dir: Path) -> None:
    target = print_dir / _ANNOTATIONS
    data = _load_yaml_file(target)
    data["columns"]["vault_id"] = {"values": [{"value": 99, "note": "stale"}]}
    _write_yaml_file(target, data)

    assert [i for i in validate_print(print_dir) if i.severity == "error"] == []


class TestWrongShapeManifest:
    """A manifest that parses but is not a mapping is a finding, never a raise."""

    def test_a_document_holding_a_sequence_is_reported(self, print_dir: Path) -> None:
        (print_dir / "manifest.yaml").write_text("- one\n- two\n")
        issues = validate_print(print_dir)

        assert "schema.type-mismatch" in _codes(issues)
        assert any("list" in i.detail for i in issues)

    def test_a_document_holding_a_scalar_is_reported(self, print_dir: Path) -> None:
        (print_dir / "manifest.yaml").write_text("just a string\n")

        assert "schema.type-mismatch" in _codes(validate_print(print_dir))

    def test_a_tables_key_holding_a_sequence_names_the_key(self, print_dir: Path) -> None:
        target = print_dir / "manifest.yaml"
        data = _load_yaml_file(target)
        data["tables"] = list(data["tables"])
        _write_yaml_file(target, data)
        issues = validate_print(print_dir)

        assert "schema.type-mismatch" in _codes(issues)
        assert any(i.path == "manifest.yaml::tables" for i in issues)

    def test_an_entry_that_is_not_a_mapping_is_reported_by_the_schema(
        self,
        print_dir: Path,
    ) -> None:
        """The JSON Schema requires an object per table, so the finding has an owner."""

        target = print_dir / "manifest.yaml"
        data = _load_yaml_file(target)
        data["tables"]["seedbank.herbarium"] = "not an entry"
        _write_yaml_file(target, data)

        assert "schema.type-mismatch" in _codes(validate_print(print_dir))

    def test_an_artifacts_value_that_is_not_a_mapping_is_reported_not_raised(
        self,
        print_dir: Path,
    ) -> None:
        target = print_dir / "manifest.yaml"
        data = _load_yaml_file(target)
        data["tables"]["seedbank.accession"]["artifacts"] = 5
        _write_yaml_file(target, data)

        assert "schema.type-mismatch" in _codes(validate_print(print_dir))

    def test_the_surviving_entries_are_still_checked(self, print_dir: Path) -> None:
        """Trips only by removing the backing file: the check reads `_ARTIFACT_FILENAMES`."""

        target = print_dir / "manifest.yaml"
        data = _load_yaml_file(target)
        data["tables"]["seedbank.herbarium"] = "not an entry"
        _write_yaml_file(target, data)
        (print_dir / "seedbank/accession/statistics.yaml").unlink()

        assert "manifest.missing-artifact" in _codes(validate_print(print_dir))


class TestASchemaIssueNamesItsField:
    """A schema violation addresses its field; jsonschema names the value, never the location."""

    def test_a_nested_enum_violation_names_the_column_and_field(self, print_dir: Path) -> None:
        target = print_dir / "seedbank/accession/statistics.yaml"
        data = _load_yaml_file(target)
        # `taxon_id` may carry no `inferred` block at all (SPEC 4.1.5 withholds `numeric_string`
        # on this numeric-typed FK column) - inject the field regardless.
        data["columns"]["taxon_id"].setdefault("inferred", {})["sensitivity"] = "not_a_sensitivity"
        _write_yaml_file(target, data)
        paths = {i.path for i in validate_print(print_dir)}

        assert "seedbank/accession/statistics.yaml::columns.taxon_id.inferred.sensitivity" in paths

    def test_a_nested_type_violation_names_the_column_and_field(self, print_dir: Path) -> None:
        target = print_dir / "seedbank/accession/statistics.yaml"
        data = _load_yaml_file(target)
        data["columns"]["taxon_id"]["null_rate"] = "not a number"
        _write_yaml_file(target, data)
        paths = {i.path for i in validate_print(print_dir)}

        assert "seedbank/accession/statistics.yaml::columns.taxon_id.null_rate" in paths

    def test_an_array_member_names_its_index(self, print_dir: Path) -> None:
        target = print_dir / "seedbank/accession/statistics.yaml"
        data = _load_yaml_file(target)
        column = next(c for c in data["columns"].values() if c.get("values"))
        column["values"][0]["count"] = "not a number"
        _write_yaml_file(target, data)
        addressed = [
            i.path
            for i in validate_print(print_dir)
            if i.path.startswith("seedbank/accession/statistics.yaml::columns.")
        ]

        assert any(p.endswith(".values[0].count") for p in addressed), addressed

    def test_a_document_level_violation_keeps_the_file_path_alone(self, print_dir: Path) -> None:
        target = print_dir / "seedbank/accession/statistics.yaml"
        data = _load_yaml_file(target)
        del data["row_count"]
        _write_yaml_file(target, data)
        match = [
            i
            for i in validate_print(print_dir)
            if i.code == "schema.missing-required-field"
            and i.path == "seedbank/accession/statistics.yaml"
        ]

        assert match

    def test_the_manifest_and_the_diff_are_addressed_alike(self, print_dir: Path) -> None:
        """One code path serves all three artifacts, so one test covers the other two."""

        manifest_path = print_dir / "manifest.yaml"
        manifest = _load_yaml_file(manifest_path)
        manifest["tables"]["seedbank.accession"]["columns"] = "not a number"
        _write_yaml_file(manifest_path, manifest)

        diff_path = print_dir / "diff.yaml"
        diff = _load_yaml_file(diff_path)
        diff["summary"]["tables_added"] = "not a number"
        _write_yaml_file(diff_path, diff)
        paths = {i.path for i in validate_print(print_dir)}

        assert "manifest.yaml::tables.seedbank.accession.columns" in paths
        assert "diff.yaml::summary.tables_added" in paths

    def test_the_verdict_does_not_move(self, print_dir: Path) -> None:
        """Only where an issue says it is changes; what it says is wrong does not."""

        target = print_dir / "seedbank/accession/statistics.yaml"
        data = _load_yaml_file(target)
        data["columns"]["taxon_id"]["null_rate"] = "not a number"
        _write_yaml_file(target, data)
        issues = validate_print(print_dir)

        assert "schema.type-mismatch" in _codes(issues)
        assert all(i.severity in {"error", "warning"} for i in issues)


class TestADocumentLevelFindingKeepsTheFilePath:
    """Both checks that emit `version.unknown-format-version` must address it at the file."""

    def test_an_unknown_version_is_addressed_at_the_file_by_every_check(
        self,
        print_dir: Path,
    ) -> None:
        target = print_dir / "manifest.yaml"
        data = _load_yaml_file(target)
        data["format_version"] = 99
        _write_yaml_file(target, data)
        paths = {
            i.path for i in validate_print(print_dir) if i.code == "version.unknown-format-version"
        }

        assert paths == {"manifest.yaml"}
