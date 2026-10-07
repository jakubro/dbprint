"""The committed print a run starts from, and what the run carries of it.

Parsed once before any table is re-read; a malformed artifact degrades as `baseline.py` describes.
"""

from __future__ import annotations

import logging
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import yaml

from dbprint import __version__ as DBPRINT_VERSION
from dbprint.adapters.base import TableMeta
from dbprint.config import ConnectionConfig, StatisticsConfig, TableSettings
from dbprint.spec import artifact_yaml
from dbprint.spec.absence import Absence, block_value, column_value, read_table_block
from dbprint.spec.artifacts import declared_artifacts, walkable_tables
from dbprint.spec.fqn import split as split_fqn
from dbprint.spec.parts import display
from dbprint.spec.statistics_matrix import forbids
from . import diff as diff_module
from .baseline import (
    failed_tables,
    load_baseline_manifest,
    missing_artifacts,
)
from .catalog_only import described_without_query
from .freshness import age_days, is_stale, parse_profiled_at
from .manifest_builder import ManifestTableEntry, profiling_params_dict, statistics_params_dict
from .relationship_graph import IncomingFk, edge_detection
from .result import TableResult


_LOG = logging.getLogger(__name__)

CarryReason = Literal["fresh", "failed", "not_attempted", "out_of_scope"]
Disposition = Literal[
    "re_extracted",
    "fresh",
    "failed",
    "not_attempted",
    "out_of_scope",
    "removed",
    "missing_artifact",
]


@dataclass(frozen=True)
class CommittedColumn:
    """One committed column's facts; `stats` is the column mapping exactly as written."""

    name: str
    sql_type: str | None = None
    nullable: bool | None = None
    classification: str | None = None
    redacted: str | None = None
    sensitivity: str | None = None
    looks_like: str | None = None
    candidate_key: bool = False
    cardinality: int | None = None
    cardinality_method: str | None = None
    sketch: Mapping[str, Any] | None = None
    stats: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RecordedSettings:
    """What the committed print records about how a table was profiled.

    Both params blocks are the connection's overlaid with the entry's override; a bare stamp is UTC.
    """

    dbprint_version: str | None = None
    profiled_at: Any = None
    profiled_instant: datetime | None = None
    max_age_days: int | None = None
    statistics_params: Mapping[str, Any] = field(default_factory=dict)
    profiling_params: Mapping[str, Any] = field(default_factory=dict)
    max_rows_scanned: int | None = None
    scope: Any = None


@dataclass(frozen=True)
class CommittedTable:
    """Everything the committed print says about one table.

    Parsed files are None when absent or unreadable; `state` is the same parse projected for the diff.
    """

    fqn: str
    entry: Mapping[str, Any]
    directory: Path = Path()
    missing_artifacts: tuple[str, ...] = ()
    recorded: RecordedSettings = field(default_factory=RecordedSettings)
    statistics: Mapping[str, Any] | None = None
    relationships: Mapping[str, Any] | None = None
    columns: Mapping[str, CommittedColumn] = field(default_factory=dict)
    refers_to: tuple[Mapping[str, Any], ...] = ()
    referenced_by: tuple[IncomingFk, ...] = ()
    state: diff_module.TableState | None = None
    last_run_failed: bool = False


@dataclass(frozen=True)
class CommittedPrint:
    """One connection's committed print, keyed by the manifest's followable entries."""

    manifest: Mapping[str, Any] | None = None
    tables: Mapping[str, CommittedTable] = field(default_factory=dict)
    failed_tables: tuple[str, ...] = ()

    @classmethod
    def load(cls, prints_root: Path) -> CommittedPrint:
        """Parse the manifest and every artifact it declares, each file at most once."""

        manifest = load_baseline_manifest(prints_root)

        if manifest is None:
            return cls()

        connection_params = manifest.get("statistics_params")
        connection_profiling = manifest.get("profiling_params")
        default_collation = manifest.get("default_collation")
        failed = failed_tables(manifest)
        tables = {
            fqn: replace(
                _load_table(
                    prints_root,
                    fqn,
                    entry,
                    manifest.get("dbprint_version"),
                    connection_params if isinstance(connection_params, dict) else {},
                    connection_profiling if isinstance(connection_profiling, dict) else {},
                    default_collation if isinstance(default_collation, str) else None,
                ),
                last_run_failed=fqn in failed,
            )
            for fqn, entry in walkable_tables(manifest).items()
        }

        return cls(manifest=manifest, tables=tables, failed_tables=failed)

    def baseline_states(self) -> dict[str, diff_module.TableState] | None:
        """The diff baseline: every committed table's state, None with no usable manifest."""

        if not self.manifest:
            return None

        return {fqn: table.state for fqn, table in self.tables.items() if table.state is not None}

    def stand_ins(self, listed: Collection[str]) -> list[TableMeta]:
        """Committed tables the target did not list, as inventory stand-ins for inference."""

        return [
            TableMeta(
                fqn=fqn,
                type=table.entry.get("type", "table"),
                namespace_path=tuple(p for p in str(table.entry.get("path", "")).split("/") if p),
            )
            for fqn, table in self.tables.items()
            if fqn not in listed
        ]


@dataclass(frozen=True)
class CarriedTable:
    """A committed table this run did not re-read, and why."""

    table: CommittedTable
    reason: CarryReason


@dataclass(frozen=True)
class CarrySet:
    """Every committed table's one disposition for this run.

    `carried` reaches the next manifest; `not_reread` is every table in scope not re-extracted.
    """

    carried: tuple[CarriedTable, ...]
    removed: tuple[str, ...]
    missing_artifact: tuple[str, ...]
    not_reread: tuple[str, ...]
    dispositions: Mapping[str, Disposition]

    def disposition(self, fqn: str) -> Disposition:
        """The disposition of one committed table; KeyError for a table the print lacks."""

        return self.dispositions[fqn]


@dataclass(frozen=True)
class CarryPlan:
    """What is known before the loop: which committed tables left scope or the target."""

    committed: CommittedPrint
    listed: tuple[str, ...]
    removed: tuple[str, ...]
    out_of_scope: tuple[str, ...]
    unreadable: tuple[str, ...]

    def settle(self, results: Sequence[TableResult]) -> CarrySet:
        """Assign every committed table its disposition from this run's per-table results."""

        status = {result.fqn: result.status for result in results}
        dispositions: dict[str, Disposition] = dict.fromkeys(self.removed, "removed")
        carried: list[CarriedTable] = []
        missing: list[str] = []
        not_reread: list[str] = list(self.unreadable)
        listed_reasons: list[tuple[str, CarryReason]] = []

        for fqn in self.listed:
            if fqn not in self.committed.tables:
                continue

            outcome = status.get(fqn)

            if outcome == "ok":
                dispositions[fqn] = "re_extracted"
                continue

            not_reread.append(fqn)
            listed_reasons.append((fqn, _LISTED_REASON.get(outcome, "not_attempted")))

        out_of_scope: list[tuple[str, CarryReason]] = [
            (fqn, "out_of_scope") for fqn in self.out_of_scope
        ]
        reasons = [*listed_reasons, *out_of_scope]

        for fqn, reason in reasons:
            table = self.committed.tables[fqn]

            if table.missing_artifacts:
                dispositions[fqn] = "missing_artifact"
                missing.append(fqn)
            else:
                dispositions[fqn] = reason
                carried.append(CarriedTable(table=table, reason=reason))

        return CarrySet(
            carried=tuple(carried),
            removed=self.removed,
            missing_artifact=tuple(missing),
            not_reread=tuple(not_reread),
            dispositions=dispositions,
        )


@dataclass(frozen=True)
class RedactionMismatch:
    """A column whose committed `redacted` marker is not what the current rules resolve."""

    column: str
    recorded: str | None
    expected: str | None


@dataclass(frozen=True)
class FreshnessVerdict:
    """Whether a listed table may be skipped, and the clause that decided it."""

    fresh: bool
    reason: str


def plan_carry(
    committed: CommittedPrint,
    *,
    listed: Sequence[str],
    scope: diff_module.DiffSelectors,
    unlisted_namespaces: Collection[str] = (),
) -> CarryPlan:
    """Split the committed tables the target did not list into removed and out of scope.

    A table in a namespace the listing could not read was never asked about, so it is carried.
    """

    listed_set = set(listed)
    namespaces = {name.lower() for name in unlisted_namespaces}
    unlisted = [fqn for fqn in committed.tables if fqn not in listed_set]
    in_scope = [fqn for fqn in unlisted if scope.covers(fqn)]
    unreadable = tuple(fqn for fqn in in_scope if split_fqn(fqn)[0] in namespaces)

    return CarryPlan(
        committed=committed,
        listed=tuple(listed),
        removed=tuple(fqn for fqn in in_scope if fqn not in unreadable),
        out_of_scope=tuple(fqn for fqn in unlisted if not scope.covers(fqn)) + unreadable,
        unreadable=unreadable,
    )


def freshness(
    table: CommittedTable | None,
    settings: TableSettings,
    *,
    generated_at: str,
    conn: ConnectionConfig,
    external: bool = False,
    opt_in_only: bool = False,
) -> FreshnessVerdict:
    """Whether the committed print of a listed table still stands for this run.

    Only while young enough and recorded under the settings this run applies, redaction included.
    """

    if table is None:
        return FreshnessVerdict(fresh=False, reason="no committed print")

    if table.last_run_failed:
        return FreshnessVerdict(fresh=False, reason="the last run could not profile it")

    # A rule tightened since another release would leave a file this binary would not emit,
    # and carrying it forward publishes it as though this binary had.
    if table.recorded.dbprint_version != DBPRINT_VERSION:
        return FreshnessVerdict(
            fresh=False,
            reason=f"written by dbprint {table.recorded.dbprint_version}, not {DBPRINT_VERSION}",
        )

    age = age_days(table.recorded.profiled_at, datetime.fromisoformat(generated_at))

    if age is None:
        return FreshnessVerdict(fresh=False, reason="profiled_at unreadable")

    if is_stale(age, settings.max_age_days):
        return FreshnessVerdict(
            fresh=False,
            reason=f"{age:.1f} days old, max_age_days {settings.max_age_days}",
        )

    committed_marker = block_value(table.statistics or {}, "catalog_only") is True
    expected_marker = described_without_query(
        table.entry.get("type"),
        read_rows=settings.read_rows,
        has_columns=bool(table.columns),
        external=external,
        opt_in_only=opt_in_only,
    )

    if committed_marker != expected_marker:
        return FreshnessVerdict(fresh=False, reason="catalog_only changed since it was profiled")

    if (block_value(table.statistics or {}, "external") is True) != external:
        return FreshnessVerdict(fresh=False, reason="external changed since it was profiled")

    changed = _changed_settings(table, settings, conn)

    if changed:
        return FreshnessVerdict(fresh=False, reason=f"{changed} changed since it was profiled")

    return FreshnessVerdict(fresh=True, reason=f"younger than max_age_days {settings.max_age_days}")


def carried_entry(
    carried: CarriedTable,
    *,
    resolved_max_age_days: int | None,
    statistics_params: Mapping[str, Any] | None = None,
    profiling_params: Mapping[str, Any] | None = None,
) -> ManifestTableEntry:
    """The next manifest entry of a carried table.

    `profiled_at` and the ceiling ride over; the overrides rebase onto this run's connection blocks.
    """

    recorded = carried.table.recorded
    entry = _entry_from_payload(carried.table.fqn, carried.table.entry)

    if statistics_params is not None:
        entry = replace(
            entry,
            statistics_params=_rebased(recorded.statistics_params, statistics_params),
        )

    if profiling_params is not None and recorded.profiling_params:
        entry = replace(
            entry,
            profiling_params=_rebased(recorded.profiling_params, profiling_params),
        )

    if carried.reason == "out_of_scope" or resolved_max_age_days is None:
        return entry

    return replace(entry, max_age_days=resolved_max_age_days)


def resolved_redaction(
    conn: ConnectionConfig,
    qualified: str,
    classification: str | None,
    sensitivity: str | None,
    looks_like: str | None,
) -> str | None:
    """The primitive a column is published under: its covering rule, none without a cell value."""

    # A row forbidding the marker itself carries no cell value, so no rule resolves a primitive.
    if forbids(classification, "redacted"):
        return None

    return conn.redaction_for(qualified, sensitivity, looks_like)


def resolved_part_redaction(
    conn: ConnectionConfig,
    qualified: str,
    classification: str | None,
    column_inferred: tuple[str | None, str | None],
    part_inferred: tuple[str | None, str | None],
) -> str | None:
    """The primitive one part is published under: through its column or its own detections.

    Each pair is `(sensitivity, looks_like)`; a part with no cell value resolves none.
    """

    if forbids(classification, "redacted"):
        return None

    return conn.redaction_for_part(qualified, *column_inferred, *part_inferred)


def redaction_mismatches(
    table: CommittedTable,
    conn: ConnectionConfig,
) -> tuple[RedactionMismatch, ...]:
    """Columns whose committed `redacted` marker the connection's rules would now set otherwise.

    Recomputed from persisted classification and detection; a column never measured expects none.
    """

    out: list[RedactionMismatch] = []
    catalog_only = table.state is not None and table.state.catalog_only

    for name, column in table.columns.items():
        # A spatial column publishes no cardinality (SPEC 2.2.3) and is measured all the same.
        measured = (
            isinstance(column.cardinality, int) and not isinstance(column.cardinality, bool)
        ) or (column.classification == "spatial" and not catalog_only)
        expected = (
            resolved_redaction(
                conn,
                f"{table.fqn}.{name}",
                column.classification,
                column.sensitivity,
                column.looks_like,
            )
            if measured
            else None
        )

        if expected != column.redacted:
            out.append(RedactionMismatch(column=name, recorded=column.redacted, expected=expected))

        out.extend(_part_mismatches(conn, f"{table.fqn}.{name}", name, column))

    return tuple(out)


def _part_mismatches(
    conn: ConnectionConfig,
    qualified: str,
    name: str,
    column: CommittedColumn,
) -> list[RedactionMismatch]:
    parts = column_value(column.stats, "parts")

    if not isinstance(parts, dict):
        return []

    out: list[RedactionMismatch] = []

    for path, block in parts.items():
        if not isinstance(block, dict):
            continue

        recorded = column_value(block, "redacted")
        cardinality = column_value(block, "cardinality")
        expected = (
            resolved_part_redaction(
                conn,
                qualified,
                column_value(block, "classification"),
                (column.sensitivity, column.looks_like),
                (
                    column_value(block, "inferred.sensitivity"),
                    column_value(block, "inferred.looks_like"),
                ),
            )
            if isinstance(cardinality, int) and not isinstance(cardinality, bool)
            else None
        )

        if expected != recorded:
            out.append(
                RedactionMismatch(column=display(name, path), recorded=recorded, expected=expected),
            )

    return out


_LISTED_REASON: dict[str | None, CarryReason] = {"skipped": "fresh", "failed": "failed"}


def _load_table(
    prints_root: Path,
    fqn: str,
    entry: dict[str, Any],
    dbprint_version: Any,
    connection_params: dict[str, Any],
    connection_profiling: dict[str, Any],
    default_collation: str | None,
) -> CommittedTable:
    directory = prints_root / entry.get("path", "")
    artifacts = declared_artifacts(entry)
    statistics = _read(directory / artifacts["statistics"]) if "statistics" in artifacts else None
    relationships = (
        _read(directory / artifacts["relationships"]) if "relationships" in artifacts else None
    )
    state = diff_module.TableState(
        fqn=fqn,
        type=entry.get("type"),
        default_collation=default_collation,
    )

    if relationships is not None:
        state.relationships = _refers_to_states(relationships)

    if statistics is not None:
        _hydrate_statistics(state, statistics)

    scope = statistics.get("scope") if statistics is not None else None
    recorded = RecordedSettings(
        dbprint_version=dbprint_version,
        profiled_at=entry.get("profiled_at"),
        profiled_instant=parse_profiled_at(entry.get("profiled_at")),
        max_age_days=entry.get("max_age_days"),
        statistics_params=_overlay(connection_params, entry.get("statistics_params")),
        profiling_params=(
            _overlay(connection_profiling, entry.get("profiling_params"))
            if connection_profiling
            else {}
        ),
        max_rows_scanned=entry.get("max_rows_scanned"),
        scope=scope,
    )
    refers_to = (relationships or {}).get("refers_to") or []

    return CommittedTable(
        fqn=fqn,
        entry=entry,
        directory=directory,
        missing_artifacts=missing_artifacts(directory, artifacts),
        recorded=recorded,
        statistics=statistics,
        relationships=relationships,
        columns=_columns(statistics),
        refers_to=tuple(e for e in refers_to if isinstance(e, dict)),
        referenced_by=_incoming(relationships),
        state=state,
    )


def _changed_settings(
    table: CommittedTable,
    settings: TableSettings,
    conn: ConnectionConfig,
) -> str:
    recorded = table.recorded
    scope = recorded.scope if isinstance(recorded.scope, dict) else {}
    catalog_only = block_value(table.statistics or {}, "catalog_only") is True
    # A view has no row-count estimate, so no ceiling governed it whether or not it was read.
    ceiling = (
        None if catalog_only or table.entry.get("type") == "view" else settings.max_rows_scanned
    )
    current_params = statistics_params_dict(settings.statistics)
    # A key a print predates reads as its default, so adding one re-profiles nothing.
    recorded_params = {
        **statistics_params_dict(StatisticsConfig()),
        **recorded.statistics_params,
    }
    changed_params = sorted(
        key
        for key in current_params.keys() | recorded_params.keys()
        if current_params.get(key) != recorded_params.get(key)
    )

    if changed_params:
        return f"statistics_params ({', '.join(changed_params)})"

    if not catalog_only and settings.filter != scope.get("filter"):
        return "filter"

    if not catalog_only and ceiling is None and settings.sample != scope.get("sample"):
        return "sample"

    if ceiling != recorded.max_rows_scanned:
        return "max_rows_scanned"

    switches = [
        key
        for key, value in profiling_params_dict(conn).items()
        if _switch_governs(key, settings, catalog_only)
        and recorded.profiling_params.get(key) != value
    ]

    if switches:
        return f"profiling_params ({', '.join(switches)})"

    redaction = redaction_mismatches(table, conn)

    if redaction:
        return f"redaction ({', '.join(m.column for m in redaction)})"

    return ""


def _switch_governs(key: str, settings: TableSettings, catalog_only: bool) -> bool:
    if catalog_only:
        return key == "infer_relationships"

    if key == "materialize_sample":
        return settings.sample is not None

    if key in {"sketch_all_columns", "compute_timeline"}:
        return settings.sample is None and settings.filter is None

    return True


def _overlay(block: Mapping[str, Any], override: Any) -> dict[str, Any]:
    return {**block, **(override if isinstance(override, dict) else {})}


def _rebased(recorded: Mapping[str, Any], block: Mapping[str, Any]) -> dict[str, Any] | None:
    differing = {key: value for key, value in recorded.items() if block.get(key) != value}

    return differing or None


def _read(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None

    try:
        data = artifact_yaml.load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return None

    if isinstance(data, dict):
        return data

    if data is not None:
        _LOG.warning("ignoring %s: expected a mapping, found %s", path, type(data).__name__)

    return None


def _columns(statistics: Mapping[str, Any] | None) -> dict[str, CommittedColumn]:
    cols = (statistics or {}).get("columns")

    if not isinstance(cols, dict):
        return {}

    out: dict[str, CommittedColumn] = {}

    for name, payload in cols.items():
        if not isinstance(payload, dict):
            continue

        inferred = payload.get("inferred")
        inferred = inferred if isinstance(inferred, dict) else {}
        sketch = payload.get("sketch")
        out[name] = CommittedColumn(
            name=name,
            sql_type=payload.get("sql_type"),
            nullable=payload.get("nullable"),
            classification=payload.get("classification"),
            redacted=payload.get("redacted"),
            sensitivity=inferred.get("sensitivity"),
            looks_like=inferred.get("looks_like"),
            candidate_key=inferred.get("candidate_key") is True,
            cardinality=payload.get("cardinality"),
            cardinality_method=payload.get("cardinality_method"),
            sketch=sketch if isinstance(sketch, dict) else None,
            stats=payload,
        )

    return out


def _incoming(relationships: Mapping[str, Any] | None) -> tuple[IncomingFk, ...]:
    out: list[IncomingFk] = []

    for entry in (relationships or {}).get("referenced_by") or []:
        if not isinstance(entry, dict):
            continue

        try:
            out.append(
                IncomingFk(
                    column=tuple(entry["column"]),
                    referencer_table=entry["referencer_table"],
                    referencer_column=tuple(entry["referencer_column"]),
                    # Absent on an inferred edge (SPEC 2.3.8) - carried as None, never
                    # defaulted to a real action or to the stronger claim `declared`.
                    on_delete=entry.get("on_delete"),
                    on_update=entry.get("on_update"),
                    detection=edge_detection(entry),
                    constraint_name=entry.get("constraint_name"),
                ),
            )
        except (KeyError, TypeError):
            continue

    return tuple(out)


def _refers_to_states(relationships: Mapping[str, Any]) -> list[diff_module.FkState]:
    fks: list[diff_module.FkState] = []

    for entry in relationships.get("refers_to") or []:
        try:
            fks.append(
                diff_module.FkState(
                    source_columns=tuple(entry["column"]),
                    target_table=entry["target_table"],
                    target_columns=tuple(entry["target_column"]),
                    # Absent on an inferred edge (SPEC 2.3.8) - carried as None, never
                    # defaulted to a real action or to the stronger claim `declared`.
                    on_delete=entry.get("on_delete"),
                    on_update=entry.get("on_update"),
                    detection=edge_detection(entry),
                ),
            )
        except (KeyError, TypeError):
            continue

    return fks


def _hydrate_statistics(state: diff_module.TableState, data: Mapping[str, Any]) -> None:
    row_count = data.get("row_count")
    state.row_count = row_count if isinstance(row_count, int) else None
    row_count_method = data.get("row_count_method")
    state.row_count_method = row_count_method if isinstance(row_count_method, str) else None
    state.scoped = isinstance(data.get("scope"), dict)
    state.catalog_only = data.get("catalog_only") is True
    state.external = data.get("external") is True
    state.grain = diff_module.grain_from_block(data.get("grain"))
    # SPEC 2.2.1: a block the baseline names unmeasured contributes nothing to compare - hydrating
    # it would resurrect the "confirmed unclustered" sentinel and invent drift against a real read.
    if read_table_block(data, "physical_layout").state is not Absence.UNMEASURED:
        state.physical_layout = diff_module.physical_layout_from_block(
            data.get("physical_layout"),
        )

    if read_table_block(data, "merging").state is not Absence.UNMEASURED:
        state.merging = diff_module.merging_from_block(block_value(data, "merging"))
    depends_on = data.get("depends_on")
    state.depends_on = tuple(depends_on) if isinstance(depends_on, list) else None

    cols = data.get("columns") or {}

    if not isinstance(cols, dict):
        return

    state.statistics = diff_module.comparable_columns(cols)

    # v1 statistics.yaml carries no default, so default_known=False suppresses default drift.
    state.columns = {
        name: diff_module.ColumnState(
            name=name,
            sql_type=str(payload.get("sql_type", "")),
            nullable=bool(payload.get("nullable", False)),
            default=None,
            default_known=False,
            physical_name=_text_or_none(payload.get("physical_name")),
            collation=_text_or_none(payload.get("collation")),
        )
        for name, payload in cols.items()
        if isinstance(payload, dict)
    }


def _text_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _entry_from_payload(fqn: str, payload: Mapping[str, Any]) -> ManifestTableEntry:
    artifacts = payload.get("artifacts")
    artifacts = artifacts if isinstance(artifacts, dict) else {}
    statistics_params = payload.get("statistics_params")

    return ManifestTableEntry(
        fqn=fqn,
        type=payload.get("type", "table"),
        path=payload.get("path", ""),
        has_statistics="statistics" in artifacts,
        has_relationships="relationships" in artifacts,
        has_description="description" in artifacts,
        has_statistics_annotations="statistics_annotations" in artifacts,
        has_relationships_annotations="relationships_annotations" in artifacts,
        row_count=payload.get("row_count"),
        columns=payload.get("columns", 0),
        profiled_at=payload.get("profiled_at", ""),
        max_age_days=payload.get("max_age_days"),
        statistics_params=statistics_params if isinstance(statistics_params, dict) else None,
        max_rows_scanned=payload.get("max_rows_scanned"),
        profiling_params=(
            payload.get("profiling_params")
            if isinstance(payload.get("profiling_params"), dict)
            else None
        ),
    )
