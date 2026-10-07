"""Regenerate the print-root consumer guide (reading.md).

The guide's vocabulary and residual-traps sections are anchored to SPEC.md and checked here,
so a moved fact fails the run; the unanchored sections stay hand-written. A further check
fails the run unless the guide cites every consumer-facing MUST or `_GUIDE_EXEMPT_SECTIONS`
records why not. The file is golden-tested against a fresh run.
"""

from __future__ import annotations

import json
import re
import runpy
from pathlib import Path
from types import SimpleNamespace


# `scripts/` is not a package, so the shared parser loads by path.
spec_markdown = SimpleNamespace(**runpy.run_path(str(Path(__file__).with_name("spec_markdown.py"))))

REPO_ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = spec_markdown.SPEC_PATH
GUIDE_PATH = REPO_ROOT / "src/dbprint/engine/reading_guide.md"
RELATIONSHIPS_SCHEMA_PATH = REPO_ROOT / "src/dbprint/spec/v1/relationships.schema.json"

_ALWAYS_REQUIRED = "R"


def _classification_matrix(spec: str) -> dict[str, dict[str, str]]:
    """SPEC 2.2.3's field matrix as {classification: {field: verdict}}."""

    classifications = spec_markdown.matrix_classifications(spec)
    out: dict[str, dict[str, str]] = {c: {} for c in classifications}

    for field, verdicts in spec_markdown.matrix(spec).items():
        for cls, verdict in zip(classifications, verdicts, strict=True):
            out[cls][field] = verdict

    return out


def _require(condition: bool, message: str) -> None:
    """Fail loudly at generation time rather than shipping a claim SPEC no longer backs."""

    if not condition:
        raise AssertionError(f"reading guide anchor broke: {message}")


def _require_contains(path: Path, needle: str, message: str) -> None:
    _require(needle in path.read_text(encoding="utf-8"), f"{message} ({path})")


def _detection_values() -> list[str]:
    """`relationships.yaml`'s own `detection` enum - the producer's authoritative list."""

    schema = json.loads(RELATIONSHIPS_SCHEMA_PATH.read_text())

    return list(schema["$defs"]["Detection"]["enum"])


def _check_detection_enumeration(values: list[str], vocabulary_text: str) -> None:
    """Every `detection` value the schema allows must be named in the vocabulary sentence - a new
    value fails generation here instead of shipping a guide whose enumeration closed without it.
    """

    for value in values:
        _require(
            f"`{value}`" in vocabulary_text,
            f"relationships.schema.json's Detection enum has {value!r}, "
            "not named in the foreign_key_candidate vocabulary sentence",
        )


# One sentence per classification, in SPEC 3.2's priority order, anchored to SPEC 3's field
# matrix and - for the FK and percentile claims - to SPEC 2.3 and the adapters' methodology.
_VOCABULARY = (
    (
        "boolean",
        (
            "Carries a full `values` list — the true/false split is exact over what was "
            "scanned (see scope, below), never a frequency sample."
        ),
    ),
    (
        "json",
        (
            "Carries a distinct-value count (`cardinality`) but no `values` list and no "
            "`distribution` — the shape is unmeasured, only the count is."
        ),
    ),
    (
        "composite",
        (
            "An array, record or map the producer looked inside: no value list of its own, but a "
            "`parts` map keyed by path (`[*]`, `.sku`, `[keys]`), each part a column-shaped block "
            "over its own `occurrences`. `parts_found` above the number listed means the rest were "
            "cut or withheld; `size` is how many parts each value holds (SPEC 2.2.18)."
        ),
    ),
    (
        "spatial",
        (
            "Geometries, described but never counted or listed: no `cardinality`, no `values`. "
            "`geometry` gives the kinds, reference systems (`srids`) and coordinate dimensions "
            "with their counts, plus the empty and invalid counts; `extent` is the X/Y bounding "
            "box in the engine's own axis order, rounded outward. Every spatial column carries "
            "`inferred.sensitivity: geolocation`, and a `redacted` marker withholds `extent` "
            "alone (SPEC 2.2.4, 2.2.9)."
        ),
    ),
    (
        "vector",
        (
            "Embeddings, never counted or listed: no `cardinality`, no `values`, no `redacted` "
            "marker. `dimension` is the element count a query embedding must match (`min < max` "
            "means the column mixes dimensions), `norm` the Euclidean norm bounds of the non-zero "
            "vectors (both near 1: unit-normalized, so inner product ranks as cosine), and "
            "`zero_count` the zero vectors cosine distance is undefined on (SPEC 2.2.4)."
        ),
    ),
    (
        "foreign_key_candidate",
        (
            "Carries a foreign key on this column, the referencing side — not the target. "
            "The edge's `detection` in `relationships.yaml` is `declared` (from the catalog), `inferred` "
            "(a naming guess a database will not enforce), or `measured` (proposed from value "
            "containment between two columns' sketches) — a measured edge is a stronger claim "
            "about the data at the instant of the read, never a stronger claim about the "
            "schema than an inferred one (SPEC 2.3); its value list follows the same "
            "truncation rule as `categorical`/`text` below."
        ),
    ),
    (
        "categorical",
        (
            "A closed or sampled domain. `values_coverage == 1.0` means an exact-match "
            "predicate is complete over what was scanned (see scope, below) — anything less is a "
            "frequent-value sample, not the whole set (SPEC 2.2.3)."
        ),
    ),
    (
        "temporal",
        (
            "Percentiles here are always a value the column holds, never interpolated — every "
            "engine takes them at the nearest rank, or near it where its construct is "
            "approximate (SPEC 2.2.4). `freshness.max_age_days` clamps at `0` for a "
            "future-dated maximum (reads `live`, not negative) and is always `0` for a "
            "date-less `TIME` type — `range.max` carries the true value regardless "
            "(SPEC 2.2.4)."
        ),
    ),
    (
        "numeric",
        (
            "Percentiles interpolate on every engine but MySQL, which takes them by rank, and "
            "BigQuery, which approximates them — a `p50` is not guaranteed to be a value the "
            "column actually holds."
        ),
    ),
    (
        "binary",
        (
            "Bytes, counted but never listed: `cardinality`, `inferred.candidate_key`, a "
            "`length` in bytes and `empty_count` (zero-length values), no `values`. A binary "
            "foreign key or a low-cardinality binary column lists its values as lowercase hex "
            "with no prefix (`0aff`), the spelling every engine's binary literal accepts "
            "(SPEC 2.2.4)."
        ),
    ),
    (
        "text",
        (
            "The value list may be exhaustive or a frequent-value sample, the same rule as "
            "`categorical` — check `values_coverage` before treating an absent value as absent "
            "from the column. A column flagged `looks_like: prose` carries none of the three "
            "at all — the producer does not read the values of a prose column."
        ),
    ),
    (
        "unsupported",
        (
            "Only `sql_type`, `nullable`, `null_count`, `null_rate` and `classification` are "
            "measured (SPEC 3.3) — plus `rows_scanned` when the file's `scope` block is "
            "present. No cardinality, no values — the producer declined to profile this type "
            "at all."
        ),
    ),
)


_NOT_EMITTED = "—"  # the matrix's own "MUST NOT emit" marker (an em dash, not a hyphen)


def _check_vocabulary_anchors(matrix: dict[str, dict[str, str]]) -> None:
    _require(matrix["boolean"]["values"] == _ALWAYS_REQUIRED, "boolean no longer requires values")
    _require(
        matrix["json"]["cardinality"] == _ALWAYS_REQUIRED,
        "json no longer requires cardinality",
    )
    _require(matrix["json"]["values"] == _NOT_EMITTED, "json now emits a values list")
    _require(
        matrix["unsupported"]["cardinality"] == _NOT_EMITTED,
        "unsupported now measures cardinality",
    )
    _require(matrix["unsupported"]["values"] == _NOT_EMITTED, "unsupported now emits values")
    _require(matrix["binary"]["values"] == _NOT_EMITTED, "binary now emits a values list")
    _require("R" in matrix["binary"]["length"], "binary no longer requires length")
    _require(matrix["spatial"]["values"] == _NOT_EMITTED, "spatial now emits a values list")
    _require(
        matrix["spatial"]["cardinality"] == _NOT_EMITTED,
        "spatial now measures cardinality",
    )
    _require(
        matrix["spatial"]["geometry"] == _ALWAYS_REQUIRED,
        "spatial no longer requires geometry",
    )
    _require(matrix["composite"]["parts"] == _ALWAYS_REQUIRED, "composite no longer requires parts")
    _require(matrix["vector"]["values"] == _NOT_EMITTED, "vector now emits a values list")
    _require(
        matrix["vector"]["zero_count"] == _ALWAYS_REQUIRED,
        "vector no longer requires zero_count",
    )
    _require(
        matrix["categorical"]["values_coverage"] == _ALWAYS_REQUIRED,
        "categorical no longer requires values_coverage",
    )
    _require("R" in matrix["text"]["values"], "text no longer requires values")
    _require(
        matrix["foreign_key_candidate"]["values"] == _ALWAYS_REQUIRED,
        "foreign_key_candidate no longer requires values",
    )
    _require(
        matrix["foreign_key_candidate"]["values_coverage"] == _ALWAYS_REQUIRED,
        "foreign_key_candidate no longer requires values_coverage",
    )
    _require(
        matrix["temporal"]["freshness"] == _ALWAYS_REQUIRED,
        "temporal no longer requires freshness",
    )
    _require("R" in matrix["numeric"]["percentiles"], "numeric no longer requires percentiles")
    _require("R" in matrix["temporal"]["percentiles"], "temporal no longer requires percentiles")

    _require_contains(
        SPEC_PATH,
        "An inferred edge is a producer's claim about the schema, "
        "not a constraint the database will honour, and a consumer MUST NOT treat it as one.",
        "SPEC 2.3's inferred-edge sentence moved",
    )
    _require_contains(
        SPEC_PATH,
        "It is not a stronger CLAIM about the schema than an inferred edge is",
        "SPEC 2.3's measured-edge-not-a-schema-claim sentence moved",
    )
    _require_contains(
        SPEC_PATH,
        "Has a foreign key, declared or inferred",
        "SPEC 3.1's foreign_key_candidate direction moved",
    )
    _require_contains(
        SPEC_PATH,
        "Producers MUST emit `0` for such a column",
        "SPEC's future-dated freshness clamp moved",
    )
    _require_contains(
        SPEC_PATH,
        "Producers MUST emit `range.span_days: 0`",
        "SPEC's date-less TIME freshness rule moved",
    )
    _require_contains(
        SPEC_PATH,
        "MUST NOT emit `values`, `values_coverage` or `distribution`",
        "SPEC's prose-column exemption moved",
    )
    _require_contains(
        REPO_ROOT / "src/dbprint/adapters/snowflake/stats.py",
        "a ranked scan for temporal percentiles",
        "Snowflake's temporal-percentile-by-rank claim moved",
    )
    _require_contains(
        REPO_ROOT / "src/dbprint/adapters/postgres/stats.py",
        "percentile_disc takes any sortable type; percentile_cont only double precision",
        "Postgres's percentile_cont-vs-percentile_disc split moved",
    )
    _require_contains(
        REPO_ROOT / "src/dbprint/adapters/mysql/stats.py",
        "percentile_disc semantics",
        "MySQL's rank-based percentile claim moved",
    )
    _require_contains(
        SPEC_PATH,
        "**A temporal percentile is a value the column holds.**",
        "SPEC 2.2.4's temporal-percentile-is-held sentence moved",
    )

    for adapter, construct in (
        ("duckdb", "PERCENTILE_CONT("),
        ("redshift", "PERCENTILE_CONT("),
        ("clickhouse", "quantileExactInclusive("),
        ("databricks", "PERCENTILE({cn}"),
        ("bigquery", "APPROX_QUANTILES({cn}"),
    ):
        _require_contains(
            REPO_ROOT / f"src/dbprint/adapters/{adapter}/stats.py",
            construct,
            f"{adapter}'s numeric percentile construct moved",
        )


_TRAPS = (
    (
        "**A table missing from `tables` may still exist.** The manifest's `failed_tables` "
        "names every table the last run attempted and could not profile; one named there and "
        "absent from `tables` exists and is unprofiled, and one named there and present is "
        "an earlier run's print, dated by its own `profiled_at` (SPEC 2.5)."
    ),
    (
        "**A `p50` is not always a value the column holds.** Numeric percentiles interpolate on "
        "every engine but MySQL and BigQuery; temporal percentiles never interpolate, on any "
        "engine (SPEC 2.2.4). MySQL takes every percentile by rank."
    ),
    (
        "**An inferred edge can resolve on a name coincidence, and a measured edge is not a "
        "stronger schema claim than either.** `refers_to`/`referenced_by` entries with "
        "`detection: inferred` are a naming match, not a verified relationship; a `measured` "
        "entry is stronger evidence about the data at `profiled_at`, never a stronger claim "
        "about the schema — a consumer MAY use either as a join candidate, never as "
        "cardinality-guaranteed, and SHOULD prefer a `declared` edge over both where one "
        "exists (SPEC 2.3) — `relationships.annotations.yaml` records where a human has "
        "since rejected an inferred one."
    ),
    (
        "**`cardinality` is collation-relative.** Two prints of one logical schema, taken "
        "through different engines or different column-level collations, can legitimately "
        "disagree on a text column's distinct count for this reason alone — it is not drift."
    ),
    (
        "**`approximate` can mean two different measurements.** "
        "`cardinality_method`/`row_count_method: approximate` covers both a live sketch this "
        "run computed and a catalog estimate of unknown staleness — the field does not "
        "distinguish the two (SPEC 2.2.2)."
    ),
    (
        "**A measured `grain`, `dependencies` entry, or `null_patterns` combination is an "
        "observation, never a constraint.** Each states what held over the rows read at "
        "`profiled_at`, on the same footing as an inferred relationship — not a rule the "
        "database enforces (SPEC 2.2.10, 2.2.12, 2.2.13)."
    ),
    (
        "**`inferred.sensitivity`'s absence never means safe to publish.** It means nothing was "
        "detected; the detector does not find every sensitive column, and this specification "
        "does not require it to (SPEC 4.4.2)."
    ),
    (
        "**Where `description.md` and `statistics.yaml` disagree, use `statistics.yaml`.** "
        "The description is written by hand and may describe the table as it was before a "
        "later run recorded a schema change (SPEC 2.4)."
    ),
    (
        "**A `catalog_only` object was never queried, not measured as empty.** Its file "
        "carries the schema facts a catalog already knew and no `row_count` and no per-column "
        "measurement at all (SPEC 2.2.15). A statistic missing there was never requested — "
        "read it as neither zero nor a value withheld."
    ),
    (
        "**An `external` object's rows live in another system.** Every query against it reads "
        "that system, whatever its statistics say (SPEC 2.2.20). Beside `catalog_only` nothing "
        "was read through it; without it the statistics describe rows fetched from the other side."
    ),
    (
        "**`grain.search.exhausted: false` does not rule out a key.** It means a per-table "
        "cap cut the search short before it could test every candidate "
        "(SPEC 2.2.12) — the absence of a measured key is a gap in the search, not evidence "
        "that the table has none beyond those listed."
    ),
    (
        "**A declared artifact with no file on disk is not the same as one never declared.** "
        "A manifest entry's `artifacts` map names every artifact kind this table declares; a "
        "kind listed there whose file is absent makes the print inconsistent, and it SHOULD "
        "be treated as such — the classification or object type does not allow that absence "
        "(SPEC 2.5, 7.3)."
    ),
    (
        "**`values_coverage_method: bounded` means the coverage figure is a clamp, not a "
        "measurement.** The value list and the population it is measured against were not "
        "read at the same instant, so a `values_coverage: 1.0` under `bounded` does not state "
        "that the list is exhaustive — `measured` means the two agreed, `bounded` means the "
        "producer found them disagreeing and clamped the figure (SPEC 2.2.4)."
    ),
    (
        "**`numeric`/`temporal` carry `values` but never `values_coverage`; `frequencies` "
        "is not an omission.** The list is the same top-N fetch `distribution` is computed "
        "from, but it is never exhaustive on these two classifications, so a validator has "
        "no exhaustive list to recompute `distribution` from — `frequencies`'s four counts "
        "— `top`, `bottom`, `listed`, `total` — are what it checks instead (SPEC 2.2.4). "
        "None of the four is a share; recompute any ratio against `non_null`/`cardinality` "
        "before trusting a rounded one."
    ),
    (
        "**`unrepresentable` changes how a bound must be read, not just which fields are "
        "absent.** A temporal `min`/`max`/percentile outside the years 0001-9999 (proleptic "
        "Gregorian) is still emitted as text — the database's own rendering — but named here "
        "so a consumer feeding it to a typed parser degrades deliberately instead of "
        "crashing (SPEC 2.2.4). The marker does not state whether the value is correct."
    ),
    (
        "**`depends_on: []` and the key omitted mean different things.** A view or "
        "matview's `[]` means the catalog was read and the object reads no other object in "
        "the print; the key omitted entirely means the dependency read did not happen — no "
        "grant, no such catalog table on this engine version, or the read failed for any other "
        "reason (SPEC 2.2.17). A producer never writes `[]` for the second case, so `[]` keeps "
        "one meaning on every engine."
    ),
    (
        "**A field named in `unmeasured` was not measured this run; nothing forbids it.** "
        "Every other absence a print carries is structural — the classification forbids the "
        "field, a redaction withheld it, the type has no day to truncate to — and SPEC 7 lists "
        "each. A name in a column's `unmeasured` list (SPEC 2.2.4), or a block in the file's "
        "own (SPEC 2.2.1), states that this run issued the read and the read failed: "
        "treat that field as unknown, never as zero, none, or a property of the data. An "
        "artifact with no marker anywhere is not thereby complete — a producer that dropped a "
        "measurement silently looks identical."
    ),
    (
        "**A timeline gap is not a zero.** `timeline.buckets` lists only a day/week/month "
        "span containing at least one non-null anchor value — a span with none is absent "
        "from the list, never published as a zero-count entry, so two consecutive buckets "
        "whose `start` values are not adjacent at `unit`'s own width mark a span with no "
        "rows, not a measured zero (SPEC 2.2.16)."
    ),
)


def _check_trap_anchors() -> None:
    _require_contains(
        SPEC_PATH,
        "Distinctness is collation-relative.",
        "SPEC's collation sentence moved",
    )
    _require_contains(
        SPEC_PATH,
        "shares nothing but a name with the source",
        "SPEC's name-coincidence sentence moved",
    )
    _require_contains(
        SPEC_PATH,
        "`approximate` names more than one measurement",
        "SPEC's approximate-ambiguity sentence moved",
    )
    _require_contains(
        SPEC_PATH,
        "MUST NOT call it a key",
        "SPEC's grain-measured-not-a-key sentence moved",
    )
    _require_contains(
        SPEC_PATH,
        "MUST NOT call it a rule the database enforces",
        "SPEC's dependencies-not-a-rule sentence moved",
    )
    _require_contains(
        SPEC_PATH,
        'absence means "not detected", never "safe to publish"',
        "SPEC's sensitivity-absence sentence moved",
    )
    _require_contains(
        SPEC_PATH,
        "use the measured statistic",
        "SPEC's description.md precedence sentence moved",
    )
    _require_contains(
        SPEC_PATH,
        "states that no query was issued, never that one was attempted",
        "SPEC's catalog_only not-a-redaction sentence moved",
    )
    _require_contains(
        SPEC_PATH,
        "a per-table cap cut the search short",
        "SPEC's grain search-exhausted sentence moved",
    )
    _require_contains(
        SPEC_PATH,
        "consumers SHOULD treat the print as inconsistent",
        "SPEC's manifest-disagreement sentence moved",
    )
    _require_contains(
        SPEC_PATH,
        "the value list and the population it is measured against were not read at the "
        "same instant",
        "SPEC's values_coverage_method clamp sentence moved",
    )
    _require_contains(
        SPEC_PATH,
        "a validator has no exhaustive list to recompute `distribution` from",
        "SPEC's frequencies-substitute sentence moved",
    )
    _require_contains(
        SPEC_PATH,
        "lets a consumer feeding that value to a typed parser degrade deliberately "
        "instead of crashing",
        "SPEC's unrepresentable-degrade sentence moved",
    )
    _require_contains(
        SPEC_PATH,
        "A producer MUST NOT collapse the two: emitting `[]` for an object whose catalog "
        "was never read",
        "SPEC's depends_on two-encoding sentence moved",
    )
    _require_contains(
        SPEC_PATH,
        "A consumer reading two consecutive buckets whose `start` values are not adjacent "
        "at `unit`'s own width has found a gap",
        "SPEC's timeline-gap sentence moved",
    )
    _require_contains(
        SPEC_PATH,
        "this marker lets a producer state the true one",
        "SPEC's unmeasured-marker sentence moved",
    )
    _require_contains(
        SPEC_PATH,
        "Absence means not clustered, never not checked.",
        "SPEC's physical_layout absence sentence moved",
    )
    _require_contains(
        SPEC_PATH,
        "arithmetically impossible for a real containment",
        "SPEC's observed.coherent sentence moved",
    )
    _require_contains(
        SPEC_PATH,
        "no ratio is ever published across a mismatched pair",
        "SPEC's observed.scope_compatible sentence moved",
    )


_READING_STRATEGY_PARAGRAPHS = (
    "## Reading strategy",
    (
        "Start at `manifest.yaml` when reading a print straight off disk — it lists every table "
        "and where its artifacts live, before opening any of them. An MCP client calls the "
        "server's tools instead; the server's instructions name the tool for each question. For a broad "
        'question ("what does this warehouse track"), read manifests and DDL first; statistics '
        "are large and most of a broad question is answered by table and column names alone. For a "
        "narrow question about one table, `ddl.sql` and `statistics.yaml` together usually answer "
        "it without a live query."
    ),
    (
        "Stop reading and query the database when a question needs a value the print does not "
        "publish — an exact row, a join across a predicate no column here encodes, anything newer "
        "than `profiled_at`. The print is a snapshot; it does not replace the database. Before "
        "reading a missing field as zero, none, or unmeasured, check what its absence means: "
        "SPEC 7 names what each absence can mean."
    ),
    (
        "A file carrying a top-level `scope` block did not read the whole table — a row predicate "
        "narrowed it, or a sample bounded the cost. Every count in it except `row_count` is over "
        "`rows_scanned`, not the table (SPEC 2.2.8) — a `boolean`'s exact split and a "
        "`values_coverage: 1.0` are both exhaustive over that narrower set only, never wider than "
        "what was actually read, and `sum` is not rescalable to table grain by assuming the sample "
        "is representative: read it as a partial total, never the column's true sum."
    ),
    (
        "A `physical_layout` block declares a clustering, partitioning or sort key: `mechanism` "
        "(`cluster`, `partition` or `sort`) names the mechanism, not a judgment; `keys` is "
        "ordered, its first component pruning far more than its last; each key's `column` is what "
        "a predicate matches against, `expression` what was actually declared. Absence means the "
        "table declares none of the three, never that the block was not read — unless the file's own "
        "`unmeasured` list names the block (SPEC 2.2.11, 2.2.1)."
    ),
    (
        "A `merging` block means the table's ClickHouse engine combines rows sharing its sorting "
        "`key` only when it merges parts, so `row_count`, `cardinality` and `grain` count the "
        "stored rows, which may repeat a key: query with `FINAL` (or `GROUP BY` the key, where "
        "`one_row_per_key` is true) for the logical rows. `rows: merged` says the statistics "
        "themselves were read with `FINAL` (SPEC 2.2.19)."
    ),
    (
        "A column carrying a `redacted` marker (`mask`, `drop`, `hash`) publishes only its "
        "counts — `cardinality`, `null_rate`, `values_coverage`, `distribution` and each "
        "value's count stay true, and every statistic computed from what the values are (`mean`, "
        "`sum`, `length`, the degenerate-value counts) is withheld (SPEC 2.2.9). Do not order, "
        "compare, or do arithmetic on a bound from one: a masked maximum still looks like a "
        "maximum, and a hashed bound sorts by digest, not value. A redacted `temporal` column's "
        "`max_age_days` and `range.span_days` are floored to the nearest 90 days, under every "
        "primitive including `drop`."
    ),
    (
        "A table with no `description.md` has no human-authored context — grain, units and "
        "exclusions are then whatever the DDL and statistics alone can support. Do not infer a "
        "business rule the artifact does not state."
    ),
)
_READING_STRATEGY = "\n\n".join(_READING_STRATEGY_PARAGRAPHS) + "\n"

_QUERY_WRITING_PARAGRAPHS = (
    "## Writing a query against a printed table",
    (
        "For each table the query touches, read `ddl.sql`; the `refers_to` and `referenced_by` "
        "edges in `relationships.yaml`, which are the join paths — each marked `declared`, "
        "`inferred` or `measured` (SPEC 2.3): an inferred edge is a guess from a column name, a "
        "measured one a value containment seen at the read, neither a constraint, so prefer a "
        "declared edge where one exists; each column's `values` with its counts and `values_coverage`; "
        "and the column notes in `statistics.annotations.yaml`. Leave the rest of "
        "`statistics.yaml` — counts, ratios, percentiles, distributions — unread: they describe "
        "the data, not what a predicate needs."
    ),
    (
        "Write a literal in a listed value's exact spelling. A list at `values_coverage` `1.0` is "
        "the whole column — over the rows scanned where the file carries `scope`; below it, or on "
        "a `numeric`/`temporal` column whose `frequencies.listed` is short of `cardinality`, a "
        "phrase absent from the list is not evidence it is absent from the column. An entry "
        "carrying `spelling_of` is another spelling of the value it names (SPEC 2.2.4) — one "
        "category stored several ways, so a predicate needs every spelling in the group."
    ),
    (
        "Every number in a print is written positionally, never in exponent form (SPEC 2.2.6), so "
        "paste it into SQL as it stands. A `null_rate` or coverage below `1.0` means some rows "
        "are not covered, however close it is: a column that is 99.96% null still holds values."
    ),
    (
        "With dbprint installed, `dbprint context <table> --purpose query` renders exactly this "
        "selection off the print, join paths included, with no server running. Served over MCP, "
        "`get_table_context` with `purpose: query` is the same selection, and `resolve_value` "
        "returns how a phrase is spelled in one column; the server's instructions say when to "
        "call it."
    ),
)
_QUERY_WRITING = "\n\n".join(_QUERY_WRITING_PARAGRAPHS) + "\n"

_SIGNALS_PARAGRAPHS = (
    "## The diff, reference lists and sketches",
    (
        "`diff.yaml` is the latest structured diff only, overwritten every run (SPEC 1.2) — a "
        "column carrying many change-kind entries this run is one whose statistics moved a lot, "
        "not a history to read across prints. A column with no entries this run is not necessarily "
        "stable: `unevaluated_tables` (SPEC 2.6.4) counts objects the diff had no basis to compare "
        "at all — a plain view, or one this run did not re-read — and those produce no events "
        "either."
    ),
    (
        "`referenced_by` lists the tables whose columns reference this one. A table with a long "
        "list is referenced from many places in the schema; one with none may be a leaf table, or may lie "
        "outside every other table's selectors (SPEC 2.3.6) — `eligible_target` on the target and "
        "the manifest's own `selectors` tell the two apart."
    ),
    (
        "A `target_table`, `referencer_table` or `depends_on` entry absent from `manifest.tables` "
        "names an object outside the print. Read it as an opaque name: its periods are not a "
        "schema/table boundary you can split on, and no directory in the print stands for it (SPEC "
        "1.3)."
    ),
)
_SIGNALS = "\n\n".join(_SIGNALS_PARAGRAPHS) + "\n"

# Guide-only: the skill copy omits this paragraph.
_SIGNALS_SKETCH_PARAGRAPHS = (
    (
        "A column's `sketch` exists for a computation the producer deliberately does not run: "
        "whether its distinct values overlap another column's, across tables or across prints, "
        "with no second query against either database. `dbprint.spec.sketch` decodes it and "
        "estimates that overlap; `observed.containment`/`target_coverage` are that same estimate "
        "already computed wherever both endpoints of an edge sit in one print (SPEC 2.3.10), "
        "alongside `fanout_avg`/`fanout_max` (average and worst-case rows per distinct referencing "
        "key) and `coherent` (`false` when the child's cardinality exceeds the parent's — "
        "arithmetically impossible for a real containment). `answerable_count` is the denominator "
        "a containment ratio must be read against, not a result on its own: the ratio's error "
        "shrinks as it grows and is large when it is small. `scope_compatible: false` means "
        "the two endpoints could not be compared on equal terms at all; every other field in the "
        "block is then absent, never zero, and no ratio is published across a mismatched pair. A "
        "sketch below its own retained size is exhaustive and tests single-value membership "
        "exactly; at or above it, membership cannot be tested."
    ),
)
_SIGNALS_SKETCH = "\n" + "\n\n".join(_SIGNALS_SKETCH_PARAGRAPHS) + "\n"


_HEADING = re.compile(r"^#{2,4} (\d+(?:\.\d+)*)\.?", re.MULTILINE)
_CONSUMER_MUST = re.compile(r"[Cc]onsumers? MUST")
_CITED_SECTION = re.compile(r"SPEC ((?:\d+(?:\.\d+)*)(?:, \d+(?:\.\d+)*)*)")

# Sections carrying a consumer-facing MUST/MUST NOT the guide deliberately does not cite,
# with the reason. Add an entry rather than weakening the guard that finds one.
_GUIDE_EXEMPT_SECTIONS = {
    "1.2.1": "self-referential - reading.md does not need to tell itself not to be hand-edited",
    "2.2.7": "the empty-table case of the scope rule the guide already states generally",
    "2.6.5": "forward-compatibility across format versions, not a rule for reading one print",
    "3.4": "forward-compatibility across format versions, not a rule for reading one print",
    "4.1.6": "forward-compatibility across format versions, not a rule for reading one print",
    "5.2": "forward-compatibility across format versions, not a rule for reading one print",
    "5.3": "forward-compatibility across format versions, not a rule for reading one print",
    "6.2": "forward-compatibility across format versions, not a rule for reading one print",
    "6.8": "forward-compatibility across format versions, not a rule for reading one print",
}


def _consumer_must_sections(spec: str) -> set[str]:
    """Every SPEC subsection number holding at least one consumer-facing MUST/MUST NOT."""

    sections: set[str] = set()
    current: str | None = None

    for line in spec.splitlines():
        heading = _HEADING.match(line)

        if heading:
            current = heading.group(1)
        elif current and _CONSUMER_MUST.search(line):
            sections.add(current)

    return sections


def _cited_sections(text: str) -> set[str]:
    """Every SPEC section number `text` cites, `(SPEC 2.2.10, 2.2.12)`-style lists split out."""

    return {number.strip() for match in _CITED_SECTION.findall(text) for number in match.split(",")}


def _check_consumer_must_coverage(spec: str) -> None:
    """A consumer MUST that SPEC adds fails generation, not just gains no mention.

    Checks the hand-written guide sections against SPEC's subsection numbers, not
    `build_document()`'s output. A parent citation (`SPEC 7`) covers every subsection under it.
    """

    guide_text = "".join(sentence for _, sentence in _VOCABULARY) + "".join(_TRAPS)
    guide_text += _READING_STRATEGY + _QUERY_WRITING + _SIGNALS + _SIGNALS_SKETCH
    cited = _cited_sections(guide_text)
    required = _consumer_must_sections(spec)
    uncovered = sorted(
        section
        for section in required
        if section not in cited
        and not any(section.startswith(f"{c}.") for c in cited)
        and section not in _GUIDE_EXEMPT_SECTIONS
    )

    _require(
        not uncovered,
        f"SPEC {', '.join(uncovered)} states a consumer-facing MUST/MUST NOT the guide "
        "neither cites nor lists in _GUIDE_EXEMPT_SECTIONS",
    )


def build_document() -> str:
    """Return the full text of the generated consumer guide."""

    spec = SPEC_PATH.read_text()
    matrix = _classification_matrix(spec)
    _check_vocabulary_anchors(matrix)
    _check_trap_anchors()
    _check_consumer_must_coverage(spec)

    fk_candidate_sentence = next(s for name, s in _VOCABULARY if name == "foreign_key_candidate")
    _check_detection_enumeration(_detection_values(), fk_candidate_sentence)

    vocab_lines = [
        "## Vocabulary",
        "",
        "Every column carries exactly one `classification` (SPEC 3):",
        "",
    ]

    for name, sentence in _VOCABULARY:
        vocab_lines.append(f"- **`{name}`** — {sentence}")

    traps_lines = ["## Fields that are easy to misread", ""]
    traps_lines.extend(f"- {trap}" for trap in _TRAPS)

    sections = [
        "# Reading a dbprint print\n\nGenerated by dbprint — do not edit by hand.",
        "\n".join(vocab_lines),
        "\n".join(traps_lines),
        _READING_STRATEGY.rstrip(),
        _QUERY_WRITING.rstrip(),
        (_SIGNALS + _SIGNALS_SKETCH).rstrip(),
    ]

    return "\n\n".join(sections) + "\n"


def write_document() -> None:
    """Render the guide and write it to its shipped location."""

    GUIDE_PATH.parent.mkdir(parents=True, exist_ok=True)
    GUIDE_PATH.write_text(build_document())


if __name__ == "__main__":
    write_document()
    print(f"wrote {GUIDE_PATH}")
