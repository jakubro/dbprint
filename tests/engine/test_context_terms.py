"""The `## Terms` legend: every printed label is defined, and every definition is printed."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, get_args

import pytest
import yaml

from dbprint.engine import AssemblyOptions, assemble_context
from dbprint.engine.context_assembler import (
    TableArtifacts,
    _grain_summary,
    _markdown_column_values,
    _markdown_joins,
    _markdown_null_patterns,
    _markdown_parts,
    _markdown_relationships,
    _scope_summary,
    _timeline_summary,
    _values_cell,
)
from dbprint.engine.context_terms import LEGEND_HEADING, TERMS, legend, term
from dbprint.engine.notes_synthesis import Rendered, synthesize
from dbprint.spec.distribution import Distribution
from dbprint.spec.scope import scope_of
from tests._grammar import split_outside_literals


_EDGE = {"column": ["herbarium_id"], "target_table": "public.herbarium", "target_column": ["id"]}

_COLUMNS: list[dict[str, Any]] = [
    {"classification": "boolean", "values": [{"value": True, "count": 3}]},
    {"classification": "boolean"},
    {"classification": "boolean", "redacted": "mask", "values": [{"count": 3}]},
    {"classification": "boolean", "redacted": "drop", "values": [{"count": 3}]},
    {"classification": "boolean", "redacted": "hash", "values": [{"count": 3}]},
    {
        "classification": "categorical",
        "cardinality": 9,
        "values": [{"value": "a", "count": 5}],
        "values_coverage": 0.5,
        "length": {"min": 1, "max": 1, "avg": 1.0},
        "values_coverage_method": "bounded",
        "null_rate": 0.2,
        "null_count": 2,
    },
    {"classification": "categorical", "cardinality": 1, "values": [], "values_coverage": 1.0},
    {"classification": "foreign_key_candidate"},
    {
        "classification": "numeric",
        "range": {"min": 0, "max": 9},
        "percentiles": {"p50": 4},
        "mean": 4.5,
        "zero_count": 1,
        "negative_count": 1,
        "quantized_count": 9,
        "distribution": "dominant_value",
        "values": [{"value": 0, "count": 9}],
        "nullable": True,
        "physical_layout_key": True,
        "inferred": {"candidate_key": True, "epoch_unit": "seconds"},
        "unmeasured": ["p99"],
    },
    {"classification": "numeric"},
    {
        "classification": "temporal",
        "range": {"min": "2024-01-01", "max": "2024-02-01", "span_days": 31},
        "percentiles": {"p01": "2024-01-02", "p99": "2024-01-31"},
        "freshness": {"classification": "live"},
        "populated": {"from": "2024-01-01", "to": "2024-02-01"},
        "quantized_count": 3,
        "unrepresentable": ["max"],
    },
    {"classification": "temporal", "redacted": "mask", "range": {"span_days": 31}},
    {"classification": "temporal"},
    {
        "classification": "text",
        "values": [{"value": "a", "count": 1}],
        "values_coverage": 0.5,
        "empty_count": 2,
    },
    {
        "classification": "text",
        "values": [{"value": "a", "count": 1}],
        "inferred": {"looks_like_candidate": "email", "looks_like_candidate_share": 0.5},
    },
    {"classification": "text"},
    {"classification": "json", "types": {"OBJECT": 1}, "parts_found": 3, "parts": {".a": {}}},
    {"classification": "composite", "parts_found": 0, "parts": {}, "empty_count": 1},
    {"classification": "binary", "sql_type": "bytea"},
    {
        "classification": "spatial",
        "geometry": {"kinds": [{"kind": "point", "count": 1}], "srids": [{"srid": 4326}]},
        "extent": {"min_x": 0, "min_y": 0, "max_x": 1, "max_y": 1},
    },
    {
        "classification": "vector",
        "dimension": {"min": 3, "max": 3},
        "norm": {"min": 1.0, "max": 1.0},
        "zero_count": 1,
    },
    {"classification": "vector", "dimension": {"min": 2, "max": 5}},
]


def _family(prefix: str) -> list[str]:
    return [key.partition(":")[2] for key in TERMS if key.startswith(f"{prefix}:")]


def _notes_battery() -> list[tuple[Rendered, dict[str, Any]]]:
    columns = [dict(column, classification=column["classification"]) for column in _COLUMNS]
    columns += [
        {"classification": "categorical", "cardinality": 2, "distribution": value}
        for value in get_args(Distribution)
    ]
    columns += [
        {"classification": "numeric", "inferred": {"looks_like": value}}
        for value in _family("looks_like")
    ]
    columns += [
        {"classification": "numeric", "inferred": {"sensitivity": value}}
        for value in _family("sensitivity")
    ]
    columns += [
        {"classification": "numeric", "inferred": {"epoch_unit": value}}
        for value in _family("epoch_unit")
    ]
    columns += [
        {
            "classification": "numeric",
            "inferred": {"candidate_key": True, "candidate_key_exception": value},
        }
        for value in _family("candidate_key_exception")
    ]
    columns += [
        {"classification": "temporal", "freshness": {"classification": value}}
        for value in _family("freshness")
    ]
    scope = scope_of({"scope": {"rows_scanned": 2, "sample": 0.5}})
    out = [(synthesize(column), column) for column in columns]
    out.append((synthesize(columns[6], scope=scope), columns[6]))
    out.append((synthesize(columns[8], scope=scope), columns[8]))
    out.append((synthesize(columns[5], statistics_params={"top_n_values": 1}), columns[5]))
    out += [
        (
            synthesize({"classification": "foreign_key_candidate"}, [f"public.herbarium.id ({d})"]),
            {},
        )
        for d in _family("detection")
    ]

    return out


def _artifacts(relationships: dict[str, Any]) -> TableArtifacts:
    return TableArtifacts(
        fqn="public.accession",
        table_type="table",
        row_count=10,
        column_count=1,
        ddl="",
        statistics=None,
        relationships=relationships,
        description=None,
        annotations=None,
        annotated_grain=None,
        relationship_annotations=None,
        missing=(),
        corrupted={},
        statistics_params_override=None,
    )


def _line_battery() -> list[Rendered]:
    observed = {
        "fanout_avg": 2.0,
        "fanout_max": 3,
        "target_coverage": 0.5,
        "containment": 1.0,
        "answerable_count": 4,
        "coherent": False,
    }
    edges = [
        {**_EDGE, "detection": "declared", "on_delete": "CASCADE", "observed": observed},
        {**_EDGE, "detection": "inferred", "observed": {"scope_compatible": False}},
        {**_EDGE, "detection": "measured"},
    ]
    incoming = [
        {"column": ["id"], "referencer_table": "public.sheet", "referencer_column": ["a_id"]},
    ]
    lines = _markdown_relationships(_artifacts({"refers_to": edges, "referenced_by": incoming}))
    lines += [
        _scope_summary({"row_count": 4, "scope": {"rows_scanned": 2, "sample": 0.5}}),
        _scope_summary({"row_count": 4, "scope": {"rows_scanned": 2, "filter": "id < 3"}}),
        _grain_summary(
            {"grain": {"keys": [{"columns": ["id"], "detection": "declared"}]}},
            {"keys": [{"columns": ["plot"], "note": "a plot"}]},
        ),
        _timeline_summary(
            {
                "timeline": {
                    "column": "sown_at",
                    "unit": "week",
                    "buckets": [{"start": "2024-01-01", "count": 2}],
                    "coverage": 1.0,
                },
            },
        ),
        _values_cell({"redacted": "mask"}, [{"count": 3}], None),
    ]
    document = {
        "classification": "json",
        "null_count": 0,
        "parts": {".a": {"classification": "numeric", "occurrences": 2}},
    }
    parted = _artifacts({})
    parted.statistics = {"row_count": 4, "columns": {"doc": document}}
    lines += _markdown_parts(parted, AssemblyOptions())
    patterned = _artifacts({})
    patterned.statistics = {
        "row_count": 4,
        "null_patterns": {"patterns": [{"columns": ["a"], "count": 4}], "coverage": 1.0},
    }
    lines += _markdown_null_patterns(patterned)
    nullable = _artifacts({})
    nullable.statistics = {
        "row_count": 4,
        "columns": {"plot": {"nullable": True, "null_count": 1, "null_rate": 0.25}},
    }
    lines += _markdown_column_values(nullable, set())
    lines += _markdown_joins(_artifacts({}))

    return lines


def _recorded() -> set[str]:
    notes = {key for rendered, _ in _notes_battery() for key in rendered.terms}

    return notes | {key for line in _line_battery() for key in line.terms}


def test_every_term_is_reachable_from_some_rendering() -> None:
    assert set(TERMS) - _recorded() == set()


_ENUM_LABELS = {"distribution", "looks like", "near", "detected", "epoch", "freshness", "redacted"}


def _labelled(fact: str) -> tuple[str, str]:
    fact = re.sub(r" over the rows scanned$", "", fact)

    if ": " not in fact:
        return re.sub(r" \(.*\)$", "", fact), ""

    label, value = fact.split(": ", 1)
    label = re.sub(r"^values \(top \d+, covering [^)]*\)$", "values (top N, covering X)", label)

    return label, re.sub(r" \(.*\)$", "", value)


@pytest.mark.parametrize("index", range(len(_notes_battery())))
def test_every_notes_label_is_a_term_the_cell_recorded(index: int) -> None:
    rendered, column = _notes_battery()[index]
    labels = {term(key).label for key in rendered.terms}
    data = {str(name) for name in column.get("types") or {}} | {
        kind["kind"] for kind in (column.get("geometry") or {}).get("kinds") or []
    }

    for fact in split_outside_literals(rendered.text, "; "):
        label, value = _labelled(fact)

        if label in data or label == column.get("sql_type"):
            continue

        assert label in labels, (fact, sorted(labels))

        if label in _ENUM_LABELS:
            assert any(value == word or value.startswith(f"{word} ") for word in labels), (
                fact,
                sorted(labels),
            )


def test_every_observed_label_is_a_term_the_line_recorded() -> None:
    lines = [line for line in _line_battery() if line.text.startswith("  observed")]

    for line in lines:
        labels = {term(key).label for key in line.terms}

        for fact in split_outside_literals(line.text.strip(), "; "):
            assert _labelled(fact)[0] in labels, (fact, sorted(labels))


def test_the_legend_is_the_heading_then_one_line_per_term_in_table_order() -> None:
    text = legend({"distribution:long_tail", "percentile:50", "detection:declared"})

    assert text.splitlines()[:2] == [LEGEND_HEADING, ""]
    assert text.splitlines()[2:] == [
        (
            "- declared: the catalog declares it - a foreign key on an edge, a primary or unique "
            "key on Grain - so it is a constraint (json/yaml `detection: declared`)"
        ),
        (
            "- long tail: the listed values cover under 30% of non-null scanned rows; many rarer "
            "values exist (json/yaml `distribution: long_tail`)"
        ),
        "- P50: the 50th percentile of the non-null scanned values (json/yaml `percentiles.p50`)",
    ]


def test_an_enum_term_filtered_by_a_tool_names_the_filter() -> None:
    assert legend({"looks_like:country_code"}).endswith(
        "(json/yaml `inferred.looks_like: country_code`; "
        "search_columns `looks_like: country_code`)",
    )


def test_no_keys_draw_no_legend() -> None:
    assert legend(set()) == ""


def _context(root: Path, tables: list[str], **options: Any) -> str:
    manifest = yaml.safe_load((root / "manifest.yaml").read_text())

    return assemble_context(manifest, root, tables, AssemblyOptions(**options), "production").text


def _unprinted(labels: list[str], text: str) -> list[str]:
    """Labels the text never prints; a label's `N`/`X` placeholders match any figure."""

    def pattern(label: str) -> str:
        return re.escape(label).replace("N", r"\d+").replace("X", r"[^)]+")

    return [label for label in labels if label not in text and not re.search(pattern(label), text)]


def _legend_labels(text: str) -> tuple[list[str], str]:
    head, _, rest = text.partition(f"{LEGEND_HEADING}\n\n")
    lines = rest.splitlines()
    entries = []

    while lines and lines[0].startswith("- "):
        entries.append(lines.pop(0))

    return [e[2:].split(": ", 1)[0] for e in entries], head + "\n".join(lines)


@pytest.mark.parametrize("purpose", ["profile", "query"])
def test_a_reply_defines_only_terms_it_prints_right_after_its_header(
    committed_print: Path,
    purpose: str,
) -> None:
    text = _context(
        committed_print / "production",
        ["arboretum.seedbank.accession"],
        purpose=purpose,
    )
    labels, rest = _legend_labels(text)

    assert text.count(LEGEND_HEADING) == 1
    assert text.split("\n## ", 1)[1].startswith("Terms\n")
    assert labels
    assert _unprinted(labels, rest) == []


def test_json_carries_no_legend(committed_print: Path) -> None:
    text = _context(committed_print / "production", ["arboretum.seedbank.accession"], format="json")

    assert LEGEND_HEADING not in text


def test_a_multi_table_reply_carries_one_legend_before_the_first_table(
    committed_print: Path,
) -> None:
    text = _context(
        committed_print / "production",
        ["arboretum.seedbank.accession", "arboretum.seedbank.taxon"],
    )

    assert text.count(LEGEND_HEADING) == 1
    assert 0 < text.index(LEGEND_HEADING) < text.index("# Table:")


def test_a_budget_shrinks_the_legend_to_the_sections_it_kept(committed_print: Path) -> None:
    root = committed_print / "production"
    whole = _context(root, ["arboretum.seedbank.accession"])
    header_and_legend = whole.split("\n\n## DDL", 1)[0]
    tight = _context(root, ["arboretum.seedbank.accession"], budget=len(header_and_legend) // 4 + 2)
    labels, rest = _legend_labels(tight)

    assert "omitted:" in tight
    assert labels
    assert len(labels) < len(_legend_labels(whole)[0])
    assert _unprinted(labels, rest) == []


def test_a_budget_below_the_legend_omits_it_and_says_so(committed_print: Path) -> None:
    root = committed_print / "production"
    header = _context(root, ["arboretum.seedbank.accession"]).split(f"\n\n{LEGEND_HEADING}", 1)[0]
    tight = _context(root, ["arboretum.seedbank.accession"], budget=len(header) // 4 + 1)

    assert LEGEND_HEADING not in tight
    assert re.search(r"omitted: [^>]*\blegend\b", tight)


def test_a_scoped_value_rows_coverage_cell_records_its_clause() -> None:
    scoped = _artifacts({})
    scoped.statistics = {
        "row_count": 10,
        "scope": {"rows_scanned": 5, "sample": 0.5},
        "columns": {
            "plot": {
                "nullable": False,
                "null_count": 0,
                "null_rate": 0.0,
                "values": [{"value": "a", "count": 5}],
                "values_coverage": 1.0,
            },
        },
    }
    row = next(
        line for line in _markdown_column_values(scoped, set()) if line.text.startswith("| plot")
    )

    assert "over the rows scanned" in row.text
    assert "over_the_rows_scanned" in row.terms
