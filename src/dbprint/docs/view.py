"""Turn a parsed print into what a docs page shows.

Pure functions over the dicts `catalogue.py` reads - no Flask, no I/O. Hedges reuse
`engine.notes_synthesis`, so this surface and `dbprint context` word one field the same.
"""

from __future__ import annotations

import math
import re
from datetime import datetime
from typing import Any

import inflect

from dbprint.engine import notes_synthesis
from dbprint.engine.baseline import unmeasured_block_message
from dbprint.engine.yaml_dumper import spell_value
from dbprint.spec.absence import Absence, block_value, column_value, read_table_block
from dbprint.spec.scope import (
    SCANNED_CLAUSE,
    ScanScope,
    list_is_complete,
    qualify,
    rows_scanned,
    scope_of,
)
from dbprint.spec.value_text import spell_percent
from . import catalogue, diagram


_INFLECT = inflect.engine()


_BUCKET_LABELS: dict[str, str] = {
    "key": "FK candidate",
    "categorical": "Categorical",
    "numeric": "Numeric",
    "temporal": "Temporal",
    "text": "Text",
    "unsupported": "Other",
}
# No "identifier" bucket: SPEC 3.1 has no such classification.
_CLASS_BUCKETS: dict[str, str] = {
    "foreign_key_candidate": "key",
    "categorical": "categorical",
    "boolean": "categorical",
    "numeric": "numeric",
    "temporal": "temporal",
    "text": "text",
    "json": "text",
    "unsupported": "unsupported",
}
_BUCKET_ORDER: tuple[str, ...] = (
    "key",
    "categorical",
    "numeric",
    "temporal",
    "text",
    "unsupported",
)


def build_index_view(connections: list[catalogue.PrintConnection]) -> list[dict[str, Any]]:
    """Per-connection summary for the index page: name, manifest header, table list, and the
    tables the last run could not profile.
    """

    return [
        {
            "name": c.name,
            "manifest": c.manifest,
            "tables": c.tables,
            "failed_tables": c.failed_tables,
        }
        for c in connections
    ]


def build_schema_view(conn: catalogue.PrintConnection, schema: str) -> dict[str, Any] | None:
    """Every table in one schema plus their intra-schema relationship count."""

    tables = catalogue.tables_in_schema(conn, schema)

    if not tables:
        return None

    n_edges = 0

    for name in tables:
        relationships = catalogue.load_relationships(conn, name)

        for r in (relationships or {}).get("refers_to") or []:
            if r.get("target_table") in tables:
                n_edges += 1

    return {"tables": tables, "n_edges": n_edges}


def build_table_view(
    conn: catalogue.PrintConnection,
    artifacts: catalogue.TableArtifacts,
) -> dict[str, Any]:
    """Compose everything the table page renders from one table's parsed artifacts."""

    statistics = artifacts.statistics
    relationships = artifacts.relationships
    columns = (statistics or {}).get("columns")
    columns = columns if isinstance(columns, dict) else {}
    row_count = _row_count(statistics, artifacts.entry)
    # SPEC 2.2.15: nothing was queried, so the aggregate cards/skyline - built from
    # null_rate/cardinality_ratio defaulting to 0 - would fabricate a measurement no column
    # carries. Per-column cells already read `None` correctly; only the aggregates suppress.
    catalog_only = bool(statistics) and block_value(statistics, "catalog_only") is True
    scope = scope_of(statistics)

    targets = catalogue.leaf_targets(conn, artifacts.fqn)
    targets.update({name: f"#col-{name}" for name in columns})  # columns win on name collision
    targets = _plural_aliases(targets)

    null_patterns = null_patterns_view(statistics) if statistics else None
    annotations = annotation_view(artifacts.statistics_annotations, columns)
    # Connection-level default, overridden per table (SPEC 2.5) - the same two-level merge
    # `engine.context_assembler` applies, so the docs page and `context` cannot diverge.
    connection_params = conn.manifest.get("statistics_params")
    table_params = artifacts.entry.get("statistics_params")
    statistics_params = {
        **(connection_params if isinstance(connection_params, dict) else {}),
        **(table_params if isinstance(table_params, dict) else {}),
    }
    column_rows = [
        column_view(
            name,
            col,
            row_count,
            relationships,
            annotations,
            targets,
            null_patterns,
            statistics_params,
            scope,
        )
        for name, col in columns.items()
    ]

    skyline_coverage = None

    if catalog_only:
        skyline = []
    else:
        heights = skyline_heights(columns) if columns else {}
        skyline = [
            {"name": name, **skyline_bar(col, heights[name])}
            for name, col in skyline_order(columns)
            if name in heights
        ]

        if columns:
            skyline_coverage = {"measured": len(heights), "total": len(columns)}

    rows = relationship_rows(
        conn,
        artifacts.fqn,
        relationships,
        artifacts.relationships_annotations,
    )
    depends_on = depends_on_view(statistics) if statistics else None

    return {
        "fqn": artifacts.fqn,
        "entry": artifacts.entry,
        "adapter": conn.manifest.get("adapter"),
        "unprofiled_notice": catalogue.unprofiled_note(conn, artifacts.fqn),
        "missing_artifacts_notice": missing_artifacts_notice(artifacts.missing),
        "corrupted_artifacts_notice": corrupted_artifacts_notice(artifacts.corrupted),
        "catalog_only_notice": catalog_only_notice(statistics),
        "row_count": row_count_view(artifacts.entry, statistics),
        "grain": grain_view(statistics, artifacts.statistics_annotations) if statistics else None,
        "null_patterns": null_patterns,
        "physical_layout": physical_layout_view(statistics) if statistics else None,
        "dependencies": dependencies_view(statistics) if statistics else [],
        "unmeasured": unmeasured_view(statistics) if statistics else {},
        "timeline": timeline_view(statistics) if statistics else None,
        "depends_on": depends_on,
        "columns_empty_notice": columns_empty_notice(statistics),
        "cards": (
            summary_cards(columns, relationships, scope)
            if statistics and not catalog_only
            else None
        ),
        "cardinality": cardinality_view(columns, row_count, scope)
        if columns and not catalog_only
        else None,
        "completeness": completeness_view(columns) if columns and not catalog_only else None,
        "skyline": skyline,
        "skyline_legend": skyline_legend(),
        "skyline_coverage": skyline_coverage,
        "columns": column_rows,
        "relationships": rows,
        "diagram": diagram.build(
            artifacts.fqn,
            rows,
            conn.name,
            tuple(depends_on or ()),
            in_print=conn.tables,
        ),
        "description": linkify(artifacts.description, targets),
        "ddl": artifacts.ddl,
    }


def row_count_view(entry: dict[str, Any], statistics: dict[str, Any] | None) -> dict[str, Any]:
    """Row count and the share of it scanned - `rows_scanned` equals `row_count` with no `scope`
    (SPEC 2.2.8); catalog-only (SPEC 2.2.15) queried nothing, so it carries no share at all.
    """

    row_count = _row_count(statistics, entry)
    catalog_only = bool(statistics) and block_value(statistics, "catalog_only") is True
    scope = scope_view(statistics) if statistics else None

    if scope is not None:
        rows_scanned, share, filter_, sample = (
            scope["rows_scanned"],
            scope["share"],
            scope["filter"],
            scope["sample"],
        )
    elif (
        not catalog_only
        and statistics is not None
        and isinstance(row_count, int)
        and row_count >= 0
    ):
        rows_scanned = row_count
        share = 1.0 if row_count > 0 else None
        filter_ = None
        sample = None
    else:
        rows_scanned, share, filter_, sample = None, None, None, None

    return {
        "row_count": row_count,
        "method": block_value(statistics or {}, "row_count_method"),
        "rows_scanned": rows_scanned,
        "share": share,
        "filter": filter_,
        "sample": sample,
    }


def scope_view(statistics: dict[str, Any]) -> dict[str, Any] | None:
    """The scanned-set banner - present only when the read was narrowed.

    The share is `rows_scanned / row_count`, SPEC 2.2.8's rescaling ratio, not `scope.sample`,
    which records what was asked for rather than what was read.
    """

    scope = scope_of(statistics)

    if scope is None:
        return None

    return {
        "rows_scanned": scope.rows_scanned,
        "row_count": scope.row_count,
        "share": scope.share,
        "sample": scope.sample,
        "filter": scope.filter,
    }


def grain_view(
    statistics: dict[str, Any],
    statistics_annotations: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """What identifies a row (SPEC 2.2.12) - absent only on an artifact predating the field.

    A human-authored `grain` key (SPEC 2.7.1) rides `key_list` beside the producer's own,
    tagged `detection: annotated` - it adds a fact, never replaces the measurement.
    """

    block = block_value(statistics, "grain")

    if not isinstance(block, dict) and not statistics_annotations:
        return None

    keys = [k for k in (block.get("keys") or []) if isinstance(k, dict)] if block else []
    keys = keys + _annotated_grain_keys(statistics_annotations)
    search = block.get("search") if block else None
    exhausted = search.get("exhausted") if isinstance(search, dict) else None

    # Not "keys": Jinja resolves `.keys` to the dict's bound method before trying item access.
    return {"key_list": keys, "search_ran": search is not None, "exhausted": exhausted}


def _annotated_grain_keys(statistics_annotations: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Human-authored grain keys as `{columns, detection, note}`, `note` omitted when absent."""

    grain = (statistics_annotations or {}).get("grain")
    keys = grain.get("keys") if isinstance(grain, dict) else None

    if not isinstance(keys, list):
        return []

    result = []

    for key in keys:
        if not isinstance(key, dict):
            continue

        entry = {"columns": key.get("columns") or [], "detection": "annotated"}
        note = key.get("note")

        if isinstance(note, str) and note.strip():
            entry["note"] = note

        result.append(entry)

    return result


def null_patterns_view(statistics: dict[str, Any]) -> dict[str, Any] | None:
    """Which columns are null together (SPEC 2.2.10) - present iff any column has a null."""

    block = block_value(statistics, "null_patterns")

    if not isinstance(block, dict):
        return None

    patterns = [p for p in (block.get("patterns") or []) if isinstance(p, dict)]

    return {
        "coverage": block.get("coverage"),
        "coverage_method": block.get("coverage_method"),
        "patterns": patterns,
    }


def null_companions(null_patterns: dict[str, Any] | None, column: str) -> list[str]:
    """Other columns null on exactly the same rows as `column`.

    A `null_patterns` entry is an exact combination (SPEC 2.2.10), so only a multi-column
    entry names a companion.
    """

    companions: list[str] = []
    seen: set[str] = set()

    for pattern in (null_patterns or {}).get("patterns") or []:
        cols = pattern.get("columns") or []

        if column not in cols or len(cols) < 2:
            continue

        for other in cols:
            if other != column and other not in seen:
                seen.add(other)
                companions.append(other)

    return companions


def physical_layout_view(statistics: dict[str, Any]) -> dict[str, Any] | None:
    """The declared clustering/partitioning key (SPEC 2.2.11) - a schema fact, never a claim."""

    block = block_value(statistics, "physical_layout")

    if not isinstance(block, dict):
        return None

    keys = [k for k in (block.get("keys") or []) if isinstance(k, dict)]

    # Not "keys": Jinja resolves `.keys` to the dict's bound method before trying item access.
    return {"mechanism": block.get("mechanism"), "key_list": keys}


def dependencies_view(statistics: dict[str, Any]) -> list[dict[str, Any]]:
    """Functional dependencies measured over the scanned rows (SPEC 2.2.13)."""

    return [d for d in (block_value(statistics, "dependencies") or []) if isinstance(d, dict)]


def depends_on_view(statistics: dict[str, Any]) -> list[str] | None:
    """Objects this view/matview reads directly, catalog-derived (SPEC 2.2.17) - `None` means the
    producer could not ask, while an empty list means the catalog answered and found nothing.
    """

    block = block_value(statistics, "depends_on")

    if not isinstance(block, list):
        return None

    return [t for t in block if isinstance(t, str)]


def timeline_view(statistics: dict[str, Any]) -> dict[str, Any] | None:
    """The anchor column's activity, bucketed at an adaptive unit (SPEC 2.2.16) - absent for
    several causes, all indistinguishable to a reader by design.
    """

    block = block_value(statistics, "timeline")

    if not isinstance(block, dict):
        return None

    buckets = [b for b in (block.get("buckets") or []) if isinstance(b, dict)]

    return {
        "column": block.get("column"),
        "unit": block.get("unit"),
        "buckets": buckets,
        "coverage": block.get("coverage"),
    }


def unmeasured_view(statistics: dict[str, Any]) -> dict[str, str]:
    """Each table-level block the file's `unmeasured` list names, with its wording (SPEC 2.2.1).

    Each named block is absent from the same file, so absence alone would read as a finding.
    """

    named = block_value(statistics, "unmeasured")

    if not isinstance(named, list):
        return {}

    return {name: unmeasured_block_message(name) for name in named if isinstance(name, str)}


def missing_artifacts_notice(missing: tuple[str, ...]) -> str | None:
    """One line naming every declared kind whose file is absent (SPEC 2.5), or None."""

    if not missing:
        return None

    return f"Missing: {', '.join(missing)} (declared but missing from disk)"


def corrupted_artifacts_notice(corrupted: tuple[str, ...]) -> str | None:
    """One line naming every declared kind present on disk but unreadable, or None - distinct
    from `missing_artifacts_notice`, because a corrupt file exists and absence reads differently.
    """

    if not corrupted:
        return None

    return f"Unreadable: {', '.join(corrupted)} (present on disk, failed to parse)"


def columns_empty_notice(statistics: dict[str, Any] | None) -> str | None:
    """Why an empty `columns` map is 'not read', never 'no columns' (SPEC 2.2.7)."""

    if statistics is None:
        return None

    columns = statistics.get("columns")

    if isinstance(columns, dict) and not columns:
        return "No columns were read - the scoped read that produced this print matched no rows."

    return None


def catalog_only_notice(statistics: dict[str, Any] | None) -> str | None:
    """Why the cardinality, completeness and sensitivity cards are absent (SPEC 2.2.15).

    Without it the page cannot tell "not queried" from "no statistics at all".
    """

    if not statistics or block_value(statistics, "catalog_only") is not True:
        return None

    return "Catalog read only - no rows were queried, so cardinality is not measured here."


def summary_cards(
    columns: dict[str, Any],
    relationships: dict[str, Any] | None,
    scope: ScanScope | None = None,
) -> dict[str, Any]:
    """Cross-column summary figures (sensitivity, redaction, freshness, connections)."""

    freshest: dict[str, Any] | None = None
    freshest_value: float | None = None

    for col, s in columns.items():
        rng, fresh = column_value(s, "range"), column_value(s, "freshness")

        if not rng or not fresh:
            continue

        try:
            value = _as_number(rng["max"])
        except (ValueError, KeyError):  # unrepresentable extreme date
            continue

        if freshest_value is None or value > freshest_value:
            freshest_value = value
            freshest = {
                "column": col,
                "max": rng["max"],
                "classification": fresh["classification"],
                "clause": _clause(scope),
            }

    return {
        "n_columns": len(columns),
        "sensitive": sum(1 for s in columns.values() if column_value(s, "inferred.sensitivity")),
        "redacted": sum(1 for s in columns.values() if column_value(s, "redacted")),
        "freshest": freshest,
        "refers_to": len((relationships or {}).get("refers_to") or []),
        "referenced_by": len((relationships or {}).get("referenced_by") or []),
    }


_COMPLETENESS_BUCKETS: tuple[str, ...] = ("full", "high", "mid", "low")


def cardinality_view(
    columns: dict[str, Any],
    row_count: int | None,
    scope: ScanScope | None = None,
) -> dict[str, Any] | None:
    """Average cardinality ratio over populated rows, across every column that measures one.

    Divides by `rows_scanned - null_count`, not SPEC 2.2.2's `cardinality_ratio`, whose
    denominator counts nulls. A column with no populated rows and one with no `cardinality`
    both contribute nothing.
    """

    bars = []

    for name, stat in columns.items():
        cardinality = column_value(stat, "cardinality")

        if cardinality is None:
            continue

        population = rows_scanned(stat, scope) if scope is not None else row_count

        if not isinstance(population, int):
            continue

        populated = population - (stat.get("null_count") or 0)

        if populated > 0:
            bars.append((name, min(1.0, cardinality / populated)))

    if not bars:
        return None

    ratios = [ratio for _, ratio in bars]

    return {
        "avg": sum(ratios) / len(ratios),
        "n_columns": len(ratios),
        "n_total": len(columns),
        "bars": [
            {"name": name, "ratio": ratio, "pct": round(100 * ratio, 1)}
            for name, ratio in sorted(bars, key=lambda b: b[1], reverse=True)
        ],
    }


def completeness_view(columns: dict[str, Any]) -> dict[str, Any] | None:
    """Average completeness (`1 - null_rate`) across columns, bucketed for the card's bar."""

    values = [1 - (stat.get("null_rate") or 0) for stat in columns.values()]

    if not values:
        return None

    counts = dict.fromkeys(_COMPLETENESS_BUCKETS, 0)

    for v in values:
        if v >= 1.0:
            counts["full"] += 1
        elif v >= 0.9:
            counts["high"] += 1
        elif v >= 0.5:
            counts["mid"] += 1
        else:
            counts["low"] += 1

    return {
        "avg": sum(values) / len(values),
        "n_columns": len(values),
        "buckets": [(bucket, counts[bucket]) for bucket in _COMPLETENESS_BUCKETS],
    }


def skyline_legend() -> list[tuple[str, str]]:
    """Bucket key + display label, in legend order."""

    return [(bucket, _BUCKET_LABELS[bucket]) for bucket in _BUCKET_ORDER]


def skyline_heights(columns: dict[str, Any]) -> dict[str, float]:
    """Log-scale cardinality ratios, normalized so the most-unique column reaches 100%. A column
    with no `cardinality_ratio` is excluded, never defaulted to 0 - the caller reads the absence.
    """

    epsilon = 1e-6
    measured = {
        col: ratio
        for col, stat in columns.items()
        if (ratio := column_value(stat, "cardinality_ratio")) is not None
    }

    if not measured:
        return {}

    log_values = {col: math.log10(ratio + epsilon) for col, ratio in measured.items()}
    lo, hi = min(log_values.values()), max(log_values.values())

    if hi == lo:
        return {col: 100.0 for col in log_values}

    return {col: round(max(6.0, (v - lo) / (hi - lo) * 100), 1) for col, v in log_values.items()}


def skyline_bar(col: dict[str, Any], height: float) -> dict[str, Any]:
    """Compute one column's fingerprint-strip bar: height given, fill=completeness, hue=type."""

    fill = max(0.0, min(100.0, 100 - (col.get("null_rate") or 0) * 100))
    bucket = _CLASS_BUCKETS.get(col.get("classification"), "unsupported")

    return {"height": height, "fill": round(fill, 1), "bucket": bucket}


def skyline_order(columns: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Order columns for the skyline chart: grouped by classification bucket, then cardinality."""

    def sort_key(item: tuple[str, dict[str, Any]]) -> tuple[int, float]:
        _, col = item
        bucket = _CLASS_BUCKETS.get(col.get("classification"), "unsupported")

        return (_BUCKET_ORDER.index(bucket), -(column_value(col, "cardinality_ratio") or 0))

    return sorted(columns.items(), key=sort_key)


def cardinality_cell(
    col: dict[str, Any],
    row_count: int | None,
    scope: ScanScope | None = None,
) -> dict[str, Any] | None:
    """The distinct count, whether it saturates its population, and how it was counted."""

    cardinality = column_value(col, "cardinality")

    if cardinality is None:
        return None

    scanned = rows_scanned(col, scope)
    saturates = False

    if scope is not None:
        saturates = bool(scanned) and cardinality == scanned
    elif row_count:
        saturates = cardinality == row_count

    return {
        "value": cardinality,
        "approximate": column_value(col, "cardinality_method") == "approximate",
        "saturates": saturates,
        "normalized_cardinality": column_value(col, "normalized_cardinality"),
    }


def values_view(col: dict[str, Any], scope: ScanScope | None = None) -> dict[str, Any] | None:
    """Bar geometry for a `values` list, plus the coverage hedge - counts and coverage are true
    under every redaction primitive (SPEC 2.2.9); only the literal `value` is withheld.
    """

    values = column_value(col, "values")
    coverage = column_value(col, "values_coverage")

    if values is None and coverage is None:
        return None

    entries = [e for e in (values or []) if isinstance(e, dict)]
    top = max((e.get("count", 0) for e in entries), default=0)
    bars = [
        {
            "value": spell_value(e["value"]) if "value" in e else "(value withheld)",
            "count": e.get("count", 0),
            "pct": round(e.get("count", 0) / top * 100, 2) if top else 0.0,
        }
        for e in entries
    ]

    return {
        "bars": bars,
        "coverage": coverage,
        "coverage_text": _coverage_text(col, coverage, scope),
        "coverage_method": column_value(col, "values_coverage_method"),
    }


def range_view(col: dict[str, Any], scope: ScanScope | None = None) -> dict[str, Any] | None:
    """Range, percentiles, mean/sum, freshness and frequencies for a numeric/temporal column -
    a `redacted` marker suppresses the box geometry and percentile list, but not aggregates.
    """

    rng = column_value(col, "range")
    percentiles = column_value(col, "percentiles") or {}
    freshness = column_value(col, "freshness")
    frequencies = column_value(col, "frequencies")

    if not (rng or percentiles or freshness or frequencies):
        return None

    redaction = _redaction_marker(col)
    box = None
    percentile_list: list[tuple[str, Any]] = []

    if redaction is None:
        if rng and {"p25", "p50", "p75"} <= percentiles.keys():
            box = _box_geometry(rng, percentiles)

        percentile_list = sorted(percentiles.items())

    return {
        "redacted": redaction,
        "box": box,
        "percentiles": percentile_list,
        "bounds": rng if redaction is None else None,
        "unrepresentable": tuple(column_value(col, "unrepresentable") or ()),
        "mean": column_value(col, "mean"),
        "sum": column_value(col, "sum"),
        "freshness": dict(freshness) if isinstance(freshness, dict) else None,
        "freshness_clause": _clause(scope),
        "frequencies": dict(frequencies) if isinstance(frequencies, dict) else None,
    }


def sketch_available(col: dict[str, Any]) -> bool:
    """Whether a KMV sketch was computed - never the payload, which is a multi-KB blob."""

    return isinstance(column_value(col, "sketch"), dict)


def annotation_view(
    statistics_annotations: dict[str, Any] | None,
    known_columns: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Per-column human notes, filtered to columns this table's statistics still carries.

    A key naming an absent column is stale (SPEC 2.7.1); a table with no `statistics` has
    no list to check against, so every key stands.
    """

    columns = (statistics_annotations or {}).get("columns")

    if not isinstance(columns, dict):
        return {}

    if not known_columns:
        return {name: entry for name, entry in columns.items() if isinstance(entry, dict)}

    return {
        name: entry
        for name, entry in columns.items()
        if isinstance(entry, dict) and name in known_columns
    }


def column_view(
    name: str,
    col: dict[str, Any],
    row_count: int | None,
    relationships: dict[str, Any] | None,
    annotations: dict[str, dict[str, Any]],
    targets: dict[str, str],
    null_patterns: dict[str, Any] | None = None,
    statistics_params: dict[str, Any] | None = None,
    scope: ScanScope | None = None,
) -> dict[str, Any]:
    """Everything the table page needs to render one column's row and expanded detail. `notes`
    reuses `engine.notes_synthesis.synthesize` in `hints_only` mode, shaped by `statistics_params`.
    """

    fk_target = fk_target_map(relationships).get(name)
    annotation = annotations.get(name)
    note_md = annotation.get("note") if annotation else None
    claims = (annotation or {}).get("claims")
    values_notes = (annotation or {}).get("values")

    return {
        "name": name,
        "sql_type": col.get("sql_type", ""),
        "nullable": col.get("nullable", True),
        "collation": column_value(col, "collation"),
        "classification": col.get("classification", "unsupported"),
        "redacted": _redaction_marker(col),
        "cardinality": cardinality_cell(col, row_count, scope),
        "null_rate": col.get("null_rate"),
        "null_count": col.get("null_count"),
        "null_companions": null_companions(null_patterns, name),
        "zero_count": column_value(col, "zero_count"),
        "negative_count": column_value(col, "negative_count"),
        "empty_count": column_value(col, "empty_count"),
        "quantized_count": column_value(col, "quantized_count"),
        "populated": column_value(col, "populated"),
        "length": column_value(col, "length"),
        "distribution": column_value(col, "distribution"),
        # SPEC 2.2.4: names the fields this run lost, so their cells are not read as forbidden.
        "unmeasured": tuple(column_value(col, "unmeasured") or ()),
        "notes": notes_synthesis.synthesize(
            col,
            fk_target,
            hints_only=True,
            statistics_params=statistics_params,
            scope=scope,
        ),
        # Not "values": Jinja resolves `.values` to the dict's bound method before item access.
        "value_list": values_view(col, scope),
        "range": range_view(col, scope),
        "sketch_available": sketch_available(col),
        "annotation_note": linkify(note_md, targets),
        "annotation_claims": sorted(claims.items()) if isinstance(claims, dict) else [],
        "annotation_values": [
            (spell_value(v.get("value")), v.get("note"))
            for v in (values_notes or [])
            if isinstance(v, dict)
        ],
    }


def fk_target_map(relationships: dict[str, Any] | None) -> dict[str, str]:
    """Map source column -> '<target>.<column> (<detection>)' for every `refers_to` entry.

    `detection` always rides the label (SPEC 2.3: a consumer MUST NOT treat a guess as a
    constraint), defaulting to `inferred` when the field is absent.
    """

    out: dict[str, str] = {}

    for entry in (relationships or {}).get("refers_to") or []:
        cols = entry.get("column") or []
        tgt_cols = entry.get("target_column") or []
        tgt_table = entry.get("target_table") or ""
        detection = _edge_detection(entry)

        if len(cols) == 1 and len(tgt_cols) == 1:
            out[cols[0]] = f"{tgt_table}.{tgt_cols[0]} ({detection})"
        elif cols:
            joined_src = ",".join(cols)
            joined_tgt = ",".join(tgt_cols) if tgt_cols else "?"
            out[joined_src] = f"{tgt_table}.({joined_tgt}) ({detection})"

    return out


def relationship_rows(
    conn: catalogue.PrintConnection,
    fqn: str,
    relationships: dict[str, Any] | None,
    relationship_annotations: dict[str, Any] | None,
) -> dict[str, Any]:
    """Every `refers_to`/`referenced_by` edge, `detection` always stated, no filler action (SPEC
    2.3.8); `in_rows` reads the referencer's own annotations - only that table authors a rejection.
    """

    refers_to = (relationships or {}).get("refers_to") or []
    referenced_by = (relationships or {}).get("referenced_by") or []
    rejected = _rejected_edges(relationship_annotations)

    out_rows = []

    for entry in refers_to:
        rejection = rejected.get(_edge_key(entry))
        out_rows.append(
            {
                "column": entry.get("column") or [],
                "target_table": entry.get("target_table"),
                "target_column": entry.get("target_column") or [],
                "detection": _edge_detection(entry),
                "on_delete": entry.get("on_delete"),
                "constraint_name": entry.get("constraint_name"),
                "path": entry.get("path"),
                "target_path": entry.get("target_path"),
                "observed": _observed_view(entry),
                "rejected": rejection is not None,
                "rejected_note": rejection.get("note") if rejection else None,
            },
        )

    in_rows = []

    for entry in referenced_by:
        rejection = _incoming_rejection(conn, fqn, entry)
        in_rows.append(
            {
                "column": entry.get("column") or [],
                "referencer_table": entry.get("referencer_table"),
                "referencer_column": entry.get("referencer_column") or [],
                "detection": _edge_detection(entry),
                "on_delete": entry.get("on_delete"),
                "constraint_name": entry.get("constraint_name"),
                "observed": _observed_view(entry),
                "rejected": rejection is not None,
                "rejected_note": rejection.get("note") if rejection else None,
            },
        )

    return {
        "refers_to": out_rows,
        "referenced_by": in_rows,
        "eligible_target": (relationships or {}).get("eligible_target"),
    }


def linkify(text: str | None, targets: dict[str, str]) -> str | None:
    """Link word-boundary mentions of `targets` keys in markdown text.

    Splits on backtick code spans first, so a mention inside one becomes a linked code span
    rather than raw link syntax markdown will not resolve.
    """

    if not text or not targets:
        return text

    prose_pattern = re.compile(
        r"\b(" + "|".join(re.escape(k) for k in sorted(targets, key=len, reverse=True)) + r")\b",
    )
    parts = re.split(r"`([^`]*)`", text)  # alternates: prose, code-inner, prose, code-inner, ...

    for i, part in enumerate(parts):
        if i % 2 == 0:
            parts[i] = prose_pattern.sub(lambda m: f"[{m.group(0)}]({targets[m.group(0)]})", part)
        else:
            parts[i] = f"[`{part}`]({targets[part]})" if part in targets else f"`{part}`"

    return "".join(parts)


def _plural_aliases(targets: dict[str, str]) -> dict[str, str]:
    """Alias each target's other-number form to the same URL.

    A generated alias never overrides a real name - `setdefault` only fills a key `targets`
    does not already carry.
    """

    aliased = dict(targets)

    for name, url in targets.items():
        for variant in _plural_variants(name):
            aliased.setdefault(variant, url)

    return aliased


def _plural_variants(name: str) -> list[str]:
    """The other-number form of `name` - singular if it is a recognized plural, else plural.

    `singular_noun` is checked first because `plural_noun` double-pluralizes an
    already-plural word (`accounts` -> `accountss`).
    """

    singular = _INFLECT.singular_noun(name)  # False when `name` isn't a recognized plural

    if singular and singular != name:
        return [singular]

    plural = _INFLECT.plural_noun(name)

    return [plural] if plural and plural != name else []


def _edge_detection(entry: dict[str, Any]) -> str:
    """The weaker reading of an absent `detection`, which SPEC 2.3.2 requires but never defaults."""

    return entry.get("detection") or "inferred"


def _rejected_edges(
    relationship_annotations: dict[str, Any] | None,
) -> dict[tuple[Any, ...], dict[str, Any]]:
    """Rejected `refers_to` entries from `relationships.annotations.yaml`, keyed by address."""

    entries = (relationship_annotations or {}).get("refers_to") or []

    return {
        _edge_key(e): e for e in entries if isinstance(e, dict) and e.get("verdict") == "rejected"
    }


def _edge_key(entry: dict[str, Any]) -> tuple[Any, ...]:
    """The (column, target_table, target_column) triplet an edge is addressed by."""

    return (
        tuple(entry.get("column") or ()),
        entry.get("target_table"),
        tuple(entry.get("target_column") or ()),
    )


def _incoming_rejection(
    conn: catalogue.PrintConnection,
    this_fqn: str,
    entry: dict[str, Any],
) -> dict[str, Any] | None:
    """Whether the referencer's own `refers_to` entry for this edge is rejected - read from that
    table's artifacts, since a rejection is a fact about the edge's owning table, not this one.
    """

    referencer = entry.get("referencer_table")

    if not isinstance(referencer, str) or not referencer:
        return None

    referencer_artifacts = catalogue.load_table(conn, referencer)

    if referencer_artifacts is None:
        return None

    rejected = _rejected_edges(referencer_artifacts.relationships_annotations)
    key = (
        tuple(entry.get("referencer_column") or ()),
        this_fqn,
        tuple(entry.get("column") or ()),
    )

    return rejected.get(key)


def _observed_view(entry: dict[str, Any]) -> dict[str, Any] | None:
    """Join cost for one edge (SPEC 2.3.10); None means "not measured", never any other reason -
    `scope_compatible: false` is itself a measurement, so it returns a dict naming that.
    """

    observed = entry.get("observed")

    if not isinstance(observed, dict):
        return None

    if observed.get("scope_compatible") is False:
        return {"scope_compatible": False}

    fanout_avg = observed.get("fanout_avg")
    target_coverage = observed.get("target_coverage")

    if fanout_avg is None or target_coverage is None:
        return None

    return {
        "fanout_avg": fanout_avg,
        "fanout_max": observed.get("fanout_max"),
        "target_coverage": target_coverage,
        "containment": observed.get("containment"),
        "answerable_count": observed.get("answerable_count"),
        "coherent": observed.get("coherent"),
    }


def _box_geometry(rng: dict[str, Any], percentiles: dict[str, Any]) -> dict[str, Any] | None:
    """Box-plot label positions in [0, 100] and the quartile values; None when unrepresentable."""

    try:
        lo, hi = _as_number(rng["min"]), _as_number(rng["max"])
        positions = {k: _as_number(percentiles[k]) for k in ("p25", "p50", "p75")}
    except (ValueError, KeyError):  # unrepresentable extreme date
        return None

    if hi == lo:
        return None

    def pos(key: str) -> float:
        return round(max(0.0, min(100.0, (positions[key] - lo) / (hi - lo) * 100)), 2)

    return {
        "q1": pos("p25"),
        "median": pos("p50"),
        "q3": pos("p75"),
        "p25_value": percentiles["p25"],
        "p50_value": percentiles["p50"],
        "p75_value": percentiles["p75"],
    }


def _redaction_marker(col: dict[str, Any]) -> str | None:
    """The `redacted` marker naming the primitive, or None when the values are real."""

    marker = column_value(col, "redacted")

    return marker if isinstance(marker, str) and marker else None


def _as_number(value: Any) -> float:
    """Coerce a numeric or ISO-timestamp statistic value to a comparable float."""

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)

    return datetime.fromisoformat(value).timestamp()


def _row_count(statistics: dict[str, Any] | None, entry: dict[str, Any]) -> Any:
    reading = read_table_block(statistics or {}, "row_count")

    return reading.value if reading.state is Absence.PRESENT else entry.get("row_count")


def _clause(scope: ScanScope | None) -> str | None:
    return None if scope is None else SCANNED_CLAUSE


def _coverage_text(col: dict[str, Any], coverage: Any, scope: ScanScope | None) -> str | None:
    if not isinstance(coverage, (int, float)) or isinstance(coverage, bool):
        return None

    text = f"{spell_percent(coverage)} covered"

    return qualify(text, scope) if list_is_complete(col) else text
