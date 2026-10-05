"""The drift family of every fact a print records (SPEC 2.6): the one mapping consumers read.

One rule per schema field and artifact kind; a rule covers its subtree unless a child has its own.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


Family = Literal["shape", "data"]
Artifact = Literal[
    "manifest",
    "statistics",
    "relationships",
    "ddl",
    "description",
    "statistics_annotations",
    "relationships_annotations",
]


@dataclass(frozen=True)
class Shape:
    """Compared; a difference means the print no longer describes the database."""

    kinds: tuple[str, ...]


@dataclass(frozen=True)
class Data:
    """Compared; a difference is the data moving, reported under these kinds."""

    kinds: tuple[str, ...]


@dataclass(frozen=True)
class Marker:
    """Not compared itself; gates which other fields are."""

    reason: str


@dataclass(frozen=True)
class Uncompared:
    """Never compared, for the stated reason."""

    reason: str


Rule = Shape | Data | Marker | Uncompared

_STATISTIC = Data(("statistic_changed",))
_CLOCK = Uncompared("a clock reading, which moves on every run (SPEC 2.5)")
_PROVENANCE = Uncompared("which producer wrote the artifact, not a fact about the database")
_CONFIGURATION = Uncompared("producer configuration, not a fact about the database")
_HUMAN = Uncompared("human-authored; the producer never writes it (SPEC 2.4, 2.7)")
_RESTATED = Uncompared("restates a field compared where it is recorded first")
_SAMPLE_REDRAW = Uncompared("redrawn with the sample on every read, not a property of the column")
_EDGE_ADDRESS = Shape(("relationship_added", "relationship_removed"))

FIELD_RULES: dict[tuple[Artifact, str], Rule] = {
    ("manifest", "format_version"): _PROVENANCE,
    ("manifest", "generated_at"): _CLOCK,
    ("manifest", "connection"): _PROVENANCE,
    ("manifest", "adapter"): _PROVENANCE,
    ("manifest", "dbprint_version"): _PROVENANCE,
    ("manifest", "statistics_params"): _CONFIGURATION,
    ("manifest", "profiling_params"): _CONFIGURATION,
    ("manifest", "redaction_rules_configured"): _CONFIGURATION,
    ("manifest", "manifest_annotations"): _HUMAN,
    ("manifest", "failed_tables"): Uncompared(
        "records what the writing run could not profile, not a fact about the database",
    ),
    ("manifest", "selectors"): Marker("the scope a comparison is bounded to (SPEC 2.6.8)"),
    ("manifest", "default_collation"): Uncompared(
        "no connection-grain kind; each affected column reports through its effective collation",
    ),
    ("manifest", "tables"): Shape(("table_added", "table_removed")),
    ("manifest", "tables.*.type"): Shape(("table_type_changed",)),
    ("manifest", "tables.*.path"): Uncompared("derived from the FQN (SPEC 1.3)"),
    ("manifest", "tables.*.artifacts"): Uncompared(
        "describes the print's own files; a missing one is conformance's (SPEC 2.5)",
    ),
    ("manifest", "tables.*.row_count"): _RESTATED,
    ("manifest", "tables.*.columns"): _RESTATED,
    ("manifest", "tables.*.profiled_at"): _CLOCK,
    ("manifest", "tables.*.max_age_days"): _CONFIGURATION,
    ("manifest", "tables.*.max_rows_scanned"): _CONFIGURATION,
    ("manifest", "tables.*.statistics_params"): _CONFIGURATION,
    ("manifest", "tables.*.profiling_params"): _CONFIGURATION,
    ("statistics", "format_version"): _PROVENANCE,
    ("statistics", "table"): Uncompared("the table's identity, compared through the manifest key"),
    ("statistics", "profiled_at"): _CLOCK,
    ("statistics", "type"): _RESTATED,
    ("statistics", "catalog_only"): Marker("no query was issued, so nothing measured compares"),
    ("statistics", "external"): Shape(("external_changed",)),
    ("statistics", "scope"): Marker("a narrowed read; scan-scale counts stop comparing"),
    ("statistics", "unmeasured"): Marker("a block that could not be read has no reading"),
    ("statistics", "row_count"): Data(("table_row_count_changed",)),
    ("statistics", "row_count_method"): Uncompared("payload of `table_row_count_changed`"),
    ("statistics", "grain"): Shape(("grain_changed",)),
    ("statistics", "physical_layout"): Shape(("physical_layout_changed",)),
    ("statistics", "merging"): Shape(("merging_changed",)),
    ("statistics", "depends_on"): Shape(("depends_on_changed",)),
    ("statistics", "null_patterns"): Uncompared(
        "a bounded top-N over the scanned set; the counts it summarises compare per column",
    ),
    ("statistics", "dependencies"): Uncompared(
        "a bounded list over the scanned set; the counts it summarises compare per column",
    ),
    ("statistics", "timeline"): Uncompared("bucket membership churns with every insert"),
    ("statistics", "columns"): Shape(("column_added", "column_removed")),
    ("statistics", "columns.*.sql_type"): Shape(("column_type_changed",)),
    ("statistics", "columns.*.nullable"): Shape(("column_nullable_changed",)),
    ("statistics", "columns.*.physical_name"): Shape(("column_physical_name_changed",)),
    ("statistics", "columns.*.collation"): Shape(("column_collation_changed",)),
    ("statistics", "columns.*.physical_layout_key"): Uncompared(
        "restates `physical_layout.keys` (SPEC 2.2.11), compared there",
    ),
    ("statistics", "columns.*.classification"): _STATISTIC,
    ("statistics", "columns.*.null_count"): _STATISTIC,
    ("statistics", "columns.*.null_rate"): _STATISTIC,
    ("statistics", "columns.*.cardinality"): _STATISTIC,
    ("statistics", "columns.*.cardinality_ratio"): _STATISTIC,
    ("statistics", "columns.*.cardinality_method"): _STATISTIC,
    ("statistics", "columns.*.values"): _STATISTIC,
    ("statistics", "columns.*.values_coverage"): _STATISTIC,
    ("statistics", "columns.*.values_coverage_method"): _STATISTIC,
    ("statistics", "columns.*.distribution"): _STATISTIC,
    ("statistics", "columns.*.frequencies"): _STATISTIC,
    ("statistics", "columns.*.range"): _STATISTIC,
    ("statistics", "columns.*.percentiles"): _STATISTIC,
    ("statistics", "columns.*.mean"): _STATISTIC,
    ("statistics", "columns.*.sum"): _STATISTIC,
    ("statistics", "columns.*.zero_count"): _STATISTIC,
    ("statistics", "columns.*.negative_count"): _STATISTIC,
    ("statistics", "columns.*.empty_count"): _STATISTIC,
    ("statistics", "columns.*.quantized_count"): _STATISTIC,
    ("statistics", "columns.*.length"): _STATISTIC,
    ("statistics", "columns.*.populated"): _STATISTIC,
    ("statistics", "columns.*.normalized_cardinality"): _STATISTIC,
    ("statistics", "columns.*.unrepresentable"): _STATISTIC,
    ("statistics", "columns.*.redacted"): _STATISTIC,
    ("statistics", "columns.*.geometry"): _STATISTIC,
    ("statistics", "columns.*.extent"): _STATISTIC,
    ("statistics", "columns.*.dimension"): _STATISTIC,
    ("statistics", "columns.*.norm"): _STATISTIC,
    ("statistics", "columns.*.parts"): Marker(
        "walked part by part: each part both sides list compares as its own block (SPEC 2.6.6)",
    ),
    ("statistics", "columns.*.parts_found"): _STATISTIC,
    ("statistics", "columns.*.size"): _STATISTIC,
    ("statistics", "columns.*.types"): _STATISTIC,
    ("statistics", "columns.*.occurrences"): _STATISTIC,
    ("statistics", "columns.*.inferred"): _STATISTIC,
    ("statistics", "columns.*.inferred.sampled"): _SAMPLE_REDRAW,
    ("statistics", "columns.*.inferred.matched"): _SAMPLE_REDRAW,
    ("statistics", "columns.*.inferred.looks_like_candidate"): _SAMPLE_REDRAW,
    ("statistics", "columns.*.inferred.looks_like_candidate_share"): _SAMPLE_REDRAW,
    ("statistics", "columns.*.rows_scanned"): Marker("the scanned-set size a scoped read echoes"),
    ("statistics", "columns.*.unmeasured"): Marker("a field that could not be read has no reading"),
    ("statistics", "columns.*.freshness"): _CLOCK,
    ("statistics", "columns.*.sketch"): Uncompared("a `dbprint diff` run computes no sketch"),
    ("relationships", "format_version"): _PROVENANCE,
    ("relationships", "table"): Uncompared(
        "the table's identity, compared through the manifest key",
    ),
    ("relationships", "profiled_at"): _CLOCK,
    ("relationships", "eligible_target"): Uncompared(
        "an inference input; its effect reports as an inferred edge added or removed",
    ),
    ("relationships", "refers_to"): Shape(("relationship_added", "relationship_removed")),
    ("relationships", "refers_to.*.column"): _EDGE_ADDRESS,
    ("relationships", "refers_to.*.target_table"): _EDGE_ADDRESS,
    ("relationships", "refers_to.*.target_column"): _EDGE_ADDRESS,
    ("relationships", "refers_to.*.on_delete"): Shape(("relationship_modified",)),
    ("relationships", "refers_to.*.on_update"): Shape(("relationship_modified",)),
    ("relationships", "refers_to.*.detection"): Shape(("relationship_modified",)),
    ("relationships", "refers_to.*.constraint_name"): Uncompared(
        "names the constraint, not the edge; edges match on columns and target (SPEC 2.6.6)",
    ),
    ("relationships", "refers_to.*.observed"): Uncompared("sketch-derived, like a measured edge"),
    ("relationships", "refers_to.*.path"): Uncompared("never producer-emitted (SPEC 2.3.9)"),
    ("relationships", "refers_to.*.target_path"): Uncompared("never producer-emitted (SPEC 2.3.9)"),
    ("relationships", "referenced_by"): Uncompared(
        "the mirror of another table's `refers_to`, compared there",
    ),
    ("ddl", ""): Uncompared(
        "v1 does not parse DDL: a view body, a table's catalog spelling, defaults, secondary "
        "indexes and comments are compared only against a live read",
    ),
    ("description", ""): _HUMAN,
    ("statistics_annotations", ""): _HUMAN,
    ("relationships_annotations", ""): _HUMAN,
}

# Shape kinds with no committed side to hydrate: `diff` fires them against a live read only.
LIVE_ONLY_SHAPE_KINDS = frozenset(
    {"column_default_changed", "index_added", "index_removed", "index_modified", "comment_changed"},
)

DATA_CHANGE_KINDS = frozenset(
    k for r in FIELD_RULES.values() if isinstance(r, Data) for k in r.kinds
)
SHAPE_CHANGE_KINDS = (
    frozenset(k for r in FIELD_RULES.values() if isinstance(r, Shape) for k in r.kinds)
    | LIVE_ONLY_SHAPE_KINDS
)


def family_of(kind: str) -> Family:
    """The drift family of a diff change kind; KeyError for a kind no rule names."""

    if kind in DATA_CHANGE_KINDS:
        return "data"
    elif kind in SHAPE_CHANGE_KINDS:
        return "shape"
    else:
        raise KeyError(kind)


def rule_for(artifact: Artifact, path: str) -> Rule:
    """The rule governing `path`: its own, or the nearest mapped ancestor's."""

    parts = path.split(".") if path else []

    for end in range(len(parts), -1, -1):
        rule = FIELD_RULES.get((artifact, ".".join(parts[:end])))

        if rule is not None:
            return rule

    raise KeyError((artifact, path))


def column_field_rule(path: str) -> Rule:
    """The rule for a column statistic's dot-path, as `statistics.yaml` nests it under a column."""

    return rule_for("statistics", f"columns.*.{path}")
