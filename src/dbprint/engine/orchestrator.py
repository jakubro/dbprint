"""Engine - orchestrator wiring Adapter + Config + Writer into generate() / compute_diff().

Both run the same extract -> classify -> graph -> diff pipeline through one private helper,
diverging at the "write or not" boundary per ARCHITECTURE 3.
"""

from __future__ import annotations

import itertools
import logging
import time
import traceback
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import yaml

from dbprint.adapters import trace_context
from dbprint.adapters.base import (
    Adapter,
    BaseStats,
    ColumnMeta,
    ColumnProgress,
    ColumnStats,
    Dependency,
    ForeignKeyMeta,
    Freshness,
    Grain,
    GrainKey,
    IndexMeta,
    Inferred,
    NullPatterns,
    PhysicalLayout,
    TableCounts,
    TableMeta,
    TableScope,
    TableType,
    UniqueKeyMeta,
)
from dbprint.adapters.errors import QueryFailed
from dbprint.config import ConfigError, ConnectionConfig, StatisticsConfig, TableSettings, selectors

# Private names: SPEC 4.1.5's numeric-type suppression below reads the classifier's own type
# membership test directly, rather than hold a second list of numeric SQL types to drift.
from dbprint.spec.classification import (
    _NUMERIC_TYPES,
    Classification,
    _matches,
    base_type,
    classify,
    compute_candidate_key_exception,
    compute_cardinality_ratio,
    is_candidate_key,
)
from dbprint.spec.coverage import is_incoherent
from dbprint.spec.epoch import bounds_epoch_unit, sample_epoch_unit
from dbprint.spec.looks_like import detect_with_evidence
from dbprint.spec.redaction import Primitive, coarsen_day_count, redact_value
from dbprint.spec.sensitivity import detect as detect_sensitivity
from dbprint.spec.sketch import METHOD as SKETCH_METHOD
from dbprint.spec.sketch import K as SKETCH_K
from dbprint.spec.sketch import (
    SketchKind,
    answerable_count,
    answerable_subset_containment,
    decode_sketch,
    estimate_intersection,
    pack_sketch,
    sketch_kind,
)
from dbprint.spec.statistics_matrix import FORBIDDEN_FIELDS
from dbprint.spec.temporal_age import freshness_classification
from dbprint.spec.temporal_age import max_age_days as derive_max_age_days
from dbprint.spec.v1 import FORMAT_VERSION
from . import diff as diff_module
from . import inference, relationship_graph
from .baseline import (
    baseline_states_from_manifest as _baseline_states_from_manifest,
)
from .baseline import (
    declared_artifacts as _declared_artifacts,
)
from .baseline import (
    hydrate_baseline_states as _hydrate_baseline_states,
)
from .baseline import (
    load_baseline_manifest as _load_baseline_manifest,
)
from .baseline import (
    load_incoming_edges as _load_incoming_edges,
)
from .manifest_builder import ManifestTableEntry, entry_from_payload
from .manifest_builder import build as build_manifest
from .reading_guide import READING_GUIDE_FILENAME, READING_GUIDE_TEXT
from .result import (
    EXIT_CONNECTION,
    EXIT_DRIFT,
    EXIT_GENERIC,
    EXIT_OK,
    EXIT_PARTIAL,
    EXIT_TOTAL_FAILURE,
    DiffRequest,
    DiffResult,
    DiffSummary,
    GenerateRequest,
    GenerateResult,
    ProgressCallback,
    ProgressEvent,
    ProgressPhase,
    ProgressStatus,
    SketchFailure,
    SummaryCounts,
    TableResult,
    TableStatus,
)
from .writer import (
    DESCRIPTION_FILENAME,
    MANIFEST_ANNOTATIONS_FILENAME,
    RELATIONSHIPS_ANNOTATIONS_FILENAME,
    STATISTICS_ANNOTATIONS_FILENAME,
    write_atomic,
)
from .yaml_dumper import dump_yaml as _dump_yaml


_LOG = logging.getLogger(__name__)

_LOOKS_LIKE_CLASSIFICATIONS = {"categorical", "text", "foreign_key_candidate"}

# The `looks_like` pattern whose columns publish no value list (SPEC 2.2.3). A literal, not
# an import: the conformance validator states the same exemption independently.
_PROSE = "prose"

# The classifications whose SPEC 2.2.3 row carries a value list.
_VALUE_LIST_CLASSIFICATIONS = {"boolean", "categorical", "foreign_key_candidate", "text"}

# Classifications whose SPEC 2.2.3 row carries no cell values at all - no value list, no
# range, no percentiles. A cheap pre-filter, not the per-column verdict.
_NO_CELL_VALUE_CLASSIFICATIONS = {"json", "unsupported"}

# Per-table cap on measured grain candidate pairs (SPEC 2.2.12) - a producer constant,
# never a `.dbprint.yaml` key: the honesty marker it produces is not tunable.
_GRAIN_SEARCH_CAP = 8

# Per-table cap on measured dependency candidate pairs (SPEC 2.2.13). The cardinality
# prune leaves the space quadratic in the low-cardinality column count, so a cap is needed.
_DEPENDENCY_SEARCH_CAP = 300

# The same confidence bar SPEC 4.1.3 sets for `looks_like`, not a second magic number.
_DEPENDENCY_STRENGTH_THRESHOLD = 0.95

_LOW_CARDINALITY_CLASSIFICATIONS = {"categorical", "boolean"}

# Map per-table outcome to the terminal progress status (`ok` -> `done`).
_TERMINAL_STATUS: dict[TableStatus, ProgressStatus] = {
    "ok": "done",
    "failed": "failed",
    "skipped": "skipped",
}


class Engine:
    """Drives the generate / compute_diff flow for one connection.

    `connect()` happens inside `generate()`/`compute_diff()`, not at construction.
    """

    def __init__(
        self,
        adapter: Adapter,
        conn_config: ConnectionConfig,
        project_root: Path,
        *,
        target: str = "",
    ) -> None:
        self._adapter = adapter
        self._conn = conn_config
        self._project_root = project_root
        # Non-secret host/database description, logged with the run's per-connection record.
        self._target = target

    def generate(self, request: GenerateRequest | None = None) -> GenerateResult:
        """Run the full generate pipeline for this connection.

        `force` bypasses the freshness skip and `dry_run` writes nothing. CLI selectors narrow
        the config's scope but never widen it (ARCHITECTURE 6).
        """

        req = request or GenerateRequest()
        started = time.monotonic()
        generated_at = _utc_iso_now()

        prints_root = self._conn.output / self._conn.name
        baseline_manifest = _load_baseline_manifest(prints_root)
        emitter = _ProgressEmitter(req.on_progress, self._conn.name)

        emitter.connecting("start")

        try:
            self._adapter.connect()
        except Exception as exc:  # noqa: BLE001 - run-all-then-report on any driver error
            emitter.connecting("failed")
            failure = _connection_error_generate(self._conn.name, generated_at, started, exc)
            self._log_connection(0, failure.elapsed_ms, failure.exit_code)

            return failure

        emitter.connecting("done")

        per_table_results: list[TableResult] = []
        diff_dict: dict[str, Any] = _empty_diff_dict(self._conn, generated_at, self._project_root)
        not_attempted = 0
        sketch_failures: tuple[SketchFailure, ...] = ()
        # Read by exec_query's own trace record; scoped to the statements this connection runs.
        conn_token = trace_context.connection.set(self._conn.name)

        try:
            outcome = self._run_extraction(
                force=req.force,
                write_artifacts=not req.dry_run,
                baseline_manifest=baseline_manifest,
                cli_include=req.cli_include,
                cli_exclude=req.cli_exclude,
                generated_at=generated_at,
                emitter=emitter,
                fail_fast=req.fail_fast,
            )
            per_table_results = outcome.per_table_results
            diff_dict = outcome.diff_dict
            not_attempted = outcome.not_attempted
            sketch_failures = outcome.sketch_failures

            # A truncated run saw only part of the database; leaving the previous manifest
            # in place keeps the baseline truthful. Keyed on tables left unattempted, not on
            # whether the loop broke (ARCHITECTURE.md 9).
            if not req.dry_run and not outcome.not_attempted:
                self._write_manifest_artifacts(
                    prints_root,
                    outcome,
                    baseline_manifest,
                    generated_at,
                )

            emitter.finalizing("done", len(per_table_results))
        finally:
            trace_context.connection.reset(conn_token)

            try:
                self._adapter.close()
            except Exception as exc:  # noqa: BLE001 - close-time failure is uninteresting
                _LOG.warning("adapter close failed for connection %r: %s", self._conn.name, exc)

        diff_result = _summarize_diff(diff_dict)
        summary = _summarize(per_table_results)
        exit_code = _derive_generate_exit_code(
            summary,
            diff_result.has_schema_changes,
            bool(sketch_failures),
        )
        elapsed_ms = int((time.monotonic() - started) * 1000)
        self._log_connection(len(per_table_results), elapsed_ms, exit_code)

        return GenerateResult(
            connection_name=self._conn.name,
            tables=tuple(per_table_results),
            summary=summary,
            diff_summary=diff_result.summary,
            elapsed_ms=elapsed_ms,
            exit_code=exit_code,
            not_attempted=not_attempted,
            sketch_failures=sketch_failures,
        )

    def compute_diff(self, request: DiffRequest | None = None) -> DiffResult:
        """Run extract -> graph -> diff, returning the diff dict only.

        Writes nothing to disk. With no `prints/<conn>/manifest.yaml` the result is
        DiffResult(exit_code=EXIT_GENERIC, diff=empty) and the CLI surfaces the error.
        """

        req = request or DiffRequest()
        started = time.monotonic()
        generated_at = _utc_iso_now()

        prints_root = self._conn.output / self._conn.name
        baseline_manifest = _load_baseline_manifest(prints_root)

        if baseline_manifest is None:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            self._log_connection(0, elapsed_ms, EXIT_GENERIC)

            return DiffResult(
                connection_name=self._conn.name,
                diff=_empty_diff_dict(self._conn, generated_at, self._project_root),
                target_scanned_tables=0,
                elapsed_ms=elapsed_ms,
                exit_code=EXIT_GENERIC,
                failed_tables=(),
            )

        emitter = _ProgressEmitter(req.on_progress, self._conn.name)
        emitter.connecting("start")

        try:
            self._adapter.connect()
        except Exception as exc:  # noqa: BLE001 - run-all-then-report on any driver error
            emitter.connecting("failed")
            elapsed_ms = int((time.monotonic() - started) * 1000)
            self._log_connection(0, elapsed_ms, EXIT_CONNECTION)

            return DiffResult(
                connection_name=self._conn.name,
                diff=_empty_diff_dict(self._conn, generated_at, self._project_root),
                target_scanned_tables=0,
                elapsed_ms=elapsed_ms,
                exit_code=EXIT_CONNECTION,
                failed_tables=(str(exc),),
            )

        emitter.connecting("done")

        diff_dict = _empty_diff_dict(self._conn, generated_at, self._project_root)
        per_table_results: list[TableResult] = []
        per_table_meta: dict[str, _PerTableContext] = {}
        conn_token = trace_context.connection.set(self._conn.name)

        try:
            outcome = self._run_extraction(
                force=True,
                write_artifacts=False,
                baseline_manifest=baseline_manifest,
                cli_include=req.cli_include,
                cli_exclude=req.cli_exclude,
                generated_at=generated_at,
                emitter=emitter,
            )
            diff_dict = outcome.diff_dict
            per_table_results = outcome.per_table_results
            per_table_meta = outcome.per_table_meta

            emitter.finalizing("done", len(per_table_results))
        finally:
            trace_context.connection.reset(conn_token)

            try:
                self._adapter.close()
            except Exception as exc:  # noqa: BLE001 - close-time failure is uninteresting
                _LOG.warning("adapter close failed for connection %r: %s", self._conn.name, exc)

        failed = tuple(r.fqn for r in per_table_results if r.status == "failed")
        scanned = sum(1 for r in per_table_results if r.status == "ok")
        exit_code = EXIT_PARTIAL if failed else EXIT_OK

        live_statistics = {
            fqn: _statistics_payload_for_assertions(ctx) for fqn, ctx in per_table_meta.items()
        }
        elapsed_ms = int((time.monotonic() - started) * 1000)
        self._log_connection(len(per_table_results), elapsed_ms, exit_code)

        return DiffResult(
            connection_name=self._conn.name,
            diff=diff_dict,
            target_scanned_tables=scanned,
            elapsed_ms=elapsed_ms,
            exit_code=exit_code,
            failed_tables=failed,
            live_statistics=live_statistics,
        )

    def _log_connection(self, tables: int, elapsed_ms: int, exit_code: int) -> None:
        """Run-log record for this connection: what it was, how long, how it ended."""

        _LOG.info(
            "connection %r: adapter=%s target=%s tables=%d elapsed_ms=%d exit_code=%d",
            self._conn.name,
            self._conn.adapter,
            self._target,
            tables,
            elapsed_ms,
            exit_code,
        )

    # Shared extraction pipeline.

    def _run_extraction(
        self,
        *,
        force: bool,
        write_artifacts: bool,
        baseline_manifest: dict[str, Any] | None,
        cli_include: tuple[str, ...],
        cli_exclude: tuple[str, ...],
        generated_at: str,
        emitter: _ProgressEmitter | None = None,
        fail_fast: bool = False,
    ) -> _ExtractionOutcome:
        """Run list_tables -> per-table extract -> relationship graph -> diff compute.

        `write_artifacts` toggles the per-table writes and the pass-2 relationships rewrite;
        manifest.yaml and diff.yaml are the caller's. `fail_fast` stops the table loop at the
        first failure, off by default so run-all-then-report stays intact.
        """

        emitter = emitter or _ProgressEmitter(None, self._conn.name)
        prints_root = self._conn.output / self._conn.name
        # Read before the loop, which overwrites what it reads: a baseline hydrated after the
        # fact would compare the new state against itself and emit no per-table event.
        baseline_states = _baseline_states_from_manifest(baseline_manifest)
        _hydrate_baseline_states(baseline_states, prints_root, baseline_manifest)
        # Constant for the whole run; a config with no size condition issues no estimate.
        read_row_counts = self._conn.rules_read_row_counts
        # Constant for the whole run too (SPEC 2.2.2) - one catalog scalar, reused for every
        # column's omit-if-matches comparison and the manifest's own record.
        default_collation = self._adapter.default_collation()
        per_table_results: list[TableResult] = []
        per_table_refers_to: dict[str, list[ForeignKeyMeta]] = {}
        per_table_meta: dict[str, _PerTableContext] = {}
        resolved_thresholds: dict[str, int] = {}

        emitter.listing("start")
        tables = self._adapter.list_tables(
            include=list(self._conn.include),
            exclude=list(self._conn.exclude),
        )
        tables = _apply_cli_narrowing(tables, cli_include, cli_exclude)
        matched_fqns = tuple(t.fqn for t in tables)
        total = len(tables)
        emitter.listing("done", total)
        # Naming inference is global and needs the whole table universe before any one table
        # is classified: the committed print's table set, not just this run's matched tables.
        inventory = self._build_inventory(
            tables + _baseline_only_tables(baseline_manifest, matched_fqns),
            emitter,
        )

        for index, tbl in enumerate(tables, start=1):
            emitter.table_start(index, total, tbl.fqn)
            # Read by exec_query's own trace record; scoped to this table alone.
            fqn_token = trace_context.fqn.set(tbl.fqn)

            try:
                result, ctx, max_age_days, matched_rules = self._process_table(
                    tbl,
                    prints_root,
                    baseline_manifest,
                    force=force,
                    dry_run=not write_artifacts,
                    generated_at=generated_at,
                    emitter=emitter,
                    index=index,
                    total=total,
                    read_row_counts=read_row_counts,
                    inventory=inventory,
                    default_collation=default_collation,
                )
            finally:
                trace_context.fqn.reset(fqn_token)

            per_table_results.append(result)
            emitter.table_done(index, total, result, ctx)
            _log_table_result(result, ctx, matched_rules)

            if max_age_days is not None:
                resolved_thresholds[tbl.fqn] = max_age_days

            if ctx is not None:
                per_table_refers_to[tbl.fqn] = ctx.relationships
                per_table_meta[tbl.fqn] = ctx

            if fail_fast and result.status == "failed":
                break

        emitter.finalizing("start", total)

        not_attempted = total - len(per_table_results)

        include, exclude = _effective_selectors(self._conn, cli_include, cli_exclude)
        carried_matched = _carried_matched(matched_fqns, per_table_meta, baseline_manifest)
        carried_out_of_scope = _carried_out_of_scope(
            baseline_manifest,
            matched_fqns,
            include,
            exclude,
        )
        # A carried entry whose declared artifacts are missing is dropped rather than
        # re-indexed; computed once for both the manifest and the preserved edges.
        baseline_tables = (baseline_manifest or {}).get("tables") or {}
        dropped_carried = tuple(
            fqn
            for fqn in carried_matched + carried_out_of_scope
            if fqn in baseline_tables and not _artifacts_present(prints_root, baseline_tables[fqn])
        )

        for fqn in dropped_carried:
            _LOG.warning(
                "carried table %r has a missing artifact; dropping it from the manifest",
                fqn,
            )

        present_carried = tuple(
            fqn for fqn in carried_matched + carried_out_of_scope if fqn not in dropped_carried
        )

        incoming = relationship_graph.resolve(per_table_refers_to)
        # A referencer this run left alone never had its relationships.yaml rewritten, so it
        # still records what it refers to, provided its print exists (`present_carried`).
        preserved = _preserved_incoming(
            prints_root,
            baseline_manifest,
            present_carried,
        )

        for fqn, ctx in per_table_meta.items():
            ctx.referenced_by = _merge_incoming(incoming.get(fqn, []), preserved.get(fqn, []))

        # Pass 2 rewrites each relationships.yaml in full, so it needs every table to have
        # completed pass 1 - a truncated run would strip real `referenced_by` entries.
        # Sketches go first: `observed` (SPEC 2.3.10) reads each endpoint's own `sketch`.
        sketch_failures: tuple[SketchFailure, ...] = ()

        if write_artifacts and not not_attempted:
            sketch_failures = self._write_key_sketches(per_table_meta, emitter=emitter)
            self._write_relationships_artifacts(prints_root, per_table_meta, baseline_states)

        diff_dict = _compute_diff_dict(
            project_root=self._project_root,
            baseline_manifest=baseline_manifest,
            baseline_states=baseline_states,
            per_table_meta=per_table_meta,
            carried_matched=carried_matched,
            conn=self._conn,
            cli_include=cli_include,
            cli_exclude=cli_exclude,
            generated_at=generated_at,
        )

        return _ExtractionOutcome(
            per_table_results=per_table_results,
            per_table_meta=per_table_meta,
            diff_dict=diff_dict,
            not_attempted=not_attempted,
            matched_fqns=matched_fqns,
            carried_matched=carried_matched,
            carried_out_of_scope=carried_out_of_scope,
            dropped_carried=dropped_carried,
            resolved_thresholds=resolved_thresholds,
            include=tuple(include),
            exclude=tuple(exclude),
            default_collation=default_collation,
            sketch_failures=sketch_failures,
        )

    # Per-table processing.

    def _process_table(
        self,
        tbl: TableMeta,
        prints_root: Path,
        baseline_manifest: dict[str, Any] | None,
        *,
        force: bool,
        dry_run: bool,
        generated_at: str,
        emitter: _ProgressEmitter,
        index: int,
        total: int,
        read_row_counts: bool = False,
        inventory: dict[str, inference.TableInventory] | None = None,
        default_collation: str = "",
    ) -> tuple[TableResult, _PerTableContext | None, int | None, tuple[str, ...]]:
        """Extract one table, or report why it was skipped or failed.

        The third element is the freshness threshold this run resolved and the fourth the rules
        that matched (`TableSettings.matched_rules`); both are absent only when resolving the
        settings is what failed. `read_row_counts` turns on the catalog estimate a size
        condition needs.
        """

        started = time.monotonic()
        tbl_dir = prints_root / Path(*tbl.namespace_path)
        row_count_estimate: int | None = None

        # Its own `try`: a catalog read that raises has to fail this table, not the run.
        if read_row_counts:
            try:
                with _operation("estimate_row_count"):
                    row_count_estimate = self._adapter.estimate_row_count(tbl.fqn)
            except Exception as exc:  # noqa: BLE001 - run-all-then-report; fails this table only
                return (
                    TableResult(
                        fqn=tbl.fqn,
                        status="failed",
                        elapsed_ms=int((time.monotonic() - started) * 1000),
                        **_error_fields(exc),
                    ),
                    None,
                    None,
                    (),
                )

            # The catalog answered "unknown", which is not the read failing. Logged so a
            # declined-to-sample table is not mistaken for one the config never matched, and
            # only where a size condition names the table - every view lacks a row count.
            if row_count_estimate is None and self._conn.size_conditions_name(tbl.fqn):
                _LOG.warning(
                    "no row-count estimate for %r; rules carrying `min_rows` or a "
                    "`max_rows_scanned` ceiling do not apply to it",
                    tbl.fqn,
                )

        # A cascade narrowing one table two ways says nothing about the rest; that table fails.
        try:
            settings = self._conn.settings_for(tbl.fqn, row_count_estimate)
        except ConfigError as exc:
            return (
                TableResult(
                    fqn=tbl.fqn,
                    status="failed",
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                    **_error_fields(exc),
                ),
                None,
                None,
                (),
            )

        # config does not log (GUIDELINES); the engine is the one place that may warn.
        if settings.ceiling_yielded:
            _LOG.warning(
                "table %r: a row-count ceiling yields to its filter, which already narrows it",
                tbl.fqn,
            )

        if not force and _is_fresh(
            baseline_manifest,
            tbl.fqn,
            settings.max_age_days,
            generated_at,
        ):
            return (
                TableResult(
                    fqn=tbl.fqn,
                    status="skipped",
                    error=None,
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                ),
                None,
                settings.max_age_days,
                settings.matched_rules,
            )

        on_column = emitter.column_hook(index, total, tbl.fqn) if emitter.enabled else None

        try:
            ctx = self._extract_table(
                tbl,
                tbl_dir,
                generated_at,
                settings,
                on_column,
                inventory or {},
                default_collation,
                row_count_estimate,
            )
        except Exception as exc:  # noqa: BLE001 - run-all-then-report; fails this table only
            return (
                TableResult(
                    fqn=tbl.fqn,
                    status="failed",
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                    **_error_fields(exc),
                ),
                None,
                settings.max_age_days,
                settings.matched_rules,
            )

        if not dry_run:
            emitter.table_phase(index, total, tbl.fqn, "write")
            artifacts: dict[str, str | bytes] = {"ddl.sql": ctx.ddl}

            if ctx.statistics_yaml is not None:
                artifacts["statistics.yaml"] = ctx.statistics_yaml
            write_atomic(tbl_dir, artifacts)

        ctx.tbl_dir = tbl_dir
        ctx.has_description = (tbl_dir / DESCRIPTION_FILENAME).is_file()
        ctx.has_statistics_annotations = (tbl_dir / STATISTICS_ANNOTATIONS_FILENAME).is_file()
        ctx.has_relationships_annotations = (tbl_dir / RELATIONSHIPS_ANNOTATIONS_FILENAME).is_file()

        return (
            TableResult(
                fqn=tbl.fqn,
                status="ok",
                error=None,
                elapsed_ms=int((time.monotonic() - started) * 1000),
            ),
            ctx,
            settings.max_age_days,
            settings.matched_rules,
        )

    def _extract_table(
        self,
        tbl: TableMeta,
        tbl_dir: Path,
        generated_at: str,
        settings: TableSettings,
        on_column: ColumnProgress | None = None,
        inventory: dict[str, inference.TableInventory] | None = None,
        default_collation: str = "",
        row_count_estimate: int | None = None,
    ) -> _PerTableContext:
        with _operation("extract_ddl"):
            ddl = self._adapter.extract_ddl(tbl.fqn)

        with _operation("introspect_columns"):
            columns = self._columns_for(tbl.fqn, inventory)

            # A table with no columns cannot exist, so an empty result means the catalog
            # query matched nothing - otherwise an empty print reported as a success.
            if tbl.type != "view" and not columns:
                raise ValueError(
                    f"catalog returned no columns for {tbl.fqn!r}; refusing to write an empty print",
                )

        with _operation("introspect_relationships"):
            relationships = self._adapter.introspect_relationships(tbl.fqn)

        relationships = relationships + self._inferred_edges(tbl.fqn, relationships, inventory)
        eligible_target = self._eligible_target(tbl.fqn, inventory)

        with _operation("introspect_indexes"):
            indexes = self._adapter.introspect_indexes(tbl.fqn)

        with _operation("extract_comments"):
            comments = self._adapter.extract_comments(tbl.fqn)

        if tbl.type == "view":
            # No query is ever issued against a view, so its file says so (SPEC 2.2.15)
            # rather than going unwritten; the columns above are the catalog's.
            view_fk_source_columns = frozenset(c for fk in relationships for c in fk.column)
            view_statistics_yaml = _serialize_catalog_only_statistics(
                tbl.fqn,
                tbl.type,
                generated_at,
                columns,
                view_fk_source_columns,
                settings.statistics.enumeration_threshold,
            )

            return _PerTableContext(
                fqn=tbl.fqn,
                type=tbl.type,
                namespace_path=tbl.namespace_path,
                columns=columns,
                relationships=relationships,
                indexes=indexes,
                comments=comments,
                ddl=ddl,
                statistics_yaml=view_statistics_yaml,
                statistics_payload=_reread_statistics(view_statistics_yaml),
                profiled_at=generated_at,
                row_count=None,
                row_count_estimate=row_count_estimate,
                max_age_days=settings.max_age_days,
                eligible_target=eligible_target,
                has_description=False,
                has_statistics_annotations=False,
                has_relationships_annotations=False,
            )

        fk_source_columns = frozenset(c for fk in relationships for c in fk.column)

        with _operation("introspect_physical_layout"):
            physical_layout = self._adapter.introspect_physical_layout(tbl.fqn)

        with _operation("introspect_unique_keys"):
            declared_keys = self._adapter.introspect_unique_keys(tbl.fqn)

        scope = _table_scope(settings)
        # A sampling construct redraws per statement, so a sampled table is copied once and
        # every call below reads the copy. `scope` still reaches the artifact (SPEC 2.2.8).
        read_scope = self._materialize_scope(tbl.fqn, scope)

        try:
            with _operation("compute_base_statistics"):
                counts, base = self._adapter.compute_base_statistics(
                    tbl.fqn,
                    columns,
                    settings.statistics,
                    read_scope,
                )

            # Detection sits between the phases: `looks_like` decides whether a value list
            # is worth enumerating before Phase B pays for the scan.
            detected = self._detect_columns(
                tbl.fqn,
                columns,
                base,
                counts,
                fk_source_columns,
                settings,
                read_scope,
            )
            suppressed = _suppressed_columns(detected)

            with _operation("compute_column_statistics"):
                stats = self._adapter.compute_column_statistics(
                    tbl.fqn,
                    columns,
                    settings.statistics,
                    counts,
                    base,
                    fk_source_columns,
                    suppress_values=suppressed,
                    on_column=on_column,
                    scope=read_scope,
                )

            with _operation("compute_null_patterns"):
                null_patterns = self._adapter.compute_null_patterns(
                    tbl.fqn,
                    columns,
                    settings.statistics,
                    counts,
                    base,
                    read_scope,
                )

            grain = self._compute_grain(
                tbl.fqn,
                columns,
                base,
                counts,
                detected,
                declared_keys,
                read_scope,
            )

            dependencies = self._compute_dependencies(
                tbl.fqn,
                columns,
                base,
                counts,
                detected,
                read_scope,
            )
        finally:
            self._release_scope(tbl.fqn, read_scope)

        enriched = _assemble_stats(tbl.fqn, columns, stats, detected, suppressed, generated_at)
        _stamp_values_coverage_method(tbl.fqn, counts.rows_scanned, enriched)
        statistics_yaml = _serialize_statistics(
            tbl.fqn,
            tbl.type,
            generated_at,
            counts,
            enriched,
            scope,
            self._conn.redaction_salt or "",
            null_patterns,
            physical_layout,
            default_collation,
            grain,
            dependencies,
        )

        return _PerTableContext(
            fqn=tbl.fqn,
            type=tbl.type,
            namespace_path=tbl.namespace_path,
            columns=columns,
            relationships=relationships,
            indexes=indexes,
            comments=comments,
            ddl=ddl,
            statistics_yaml=statistics_yaml,
            statistics_payload=_reread_statistics(statistics_yaml),
            profiled_at=generated_at,
            row_count=counts.row_count,
            row_count_estimate=row_count_estimate,
            max_age_days=settings.max_age_days,
            rows_scanned=counts.rows_scanned,
            scope=scope,
            eligible_target=eligible_target,
            has_description=False,
            has_statistics_annotations=False,
            has_relationships_annotations=False,
            unique_keys=tuple(declared_keys),
        )

    def _build_inventory(
        self,
        tables: list[TableMeta],
        emitter: _ProgressEmitter | None = None,
    ) -> dict[str, inference.TableInventory]:
        """Read columns and declared keys for every object in `tables`, ahead of statistics.

        `tables` is the caller-assembled inference universe, not this run's matched set. A
        failed catalog read is registered rather than dropped, since dropping a name can make
        another table's stem unambiguous and manufacture an edge. A failed unique-keys read
        sets `keys_known=False`, and the warning here is its only record.
        """

        if not self._conn.infer_relationships:
            return {}

        out: dict[str, inference.TableInventory] = {}
        total = len(tables)

        if emitter is not None:
            emitter.inventory_phase("start", total)

        for index, tbl in enumerate(tables, start=1):
            if emitter is not None:
                emitter.inventory_tick(index, total, tbl.fqn)

            # Read by exec_query's own trace record; scoped to this table alone.
            fqn_token = trace_context.fqn.set(tbl.fqn)

            try:
                try:
                    with _operation("introspect_columns"):
                        columns = self._adapter.introspect_columns(tbl.fqn)
                except Exception as exc:  # noqa: BLE001 - degrade to no pre-read columns
                    _LOG.warning(
                        "catalog pre-pass introspect_columns failed for %r: %s",
                        tbl.fqn,
                        exc,
                    )
                    columns = []

                keys_known = True

                try:
                    with _operation("introspect_unique_keys"):
                        unique_keys = self._adapter.introspect_unique_keys(tbl.fqn)
                except Exception as exc:  # noqa: BLE001 - degrade to no pre-read keys
                    _LOG.warning(
                        "catalog pre-pass introspect_unique_keys failed for %r: %s",
                        tbl.fqn,
                        exc,
                    )
                    unique_keys = []
                    keys_known = False
            finally:
                trace_context.fqn.reset(fqn_token)

            out[tbl.fqn] = inference.TableInventory.from_catalog(
                tbl.fqn,
                tbl.type,
                columns,
                unique_keys,
                keys_known=keys_known,
            )

        if emitter is not None:
            emitter.inventory_phase("done", total)

        return out

    def _columns_for(
        self,
        fqn: str,
        inventory: dict[str, inference.TableInventory] | None,
    ) -> list[ColumnMeta]:
        """The pre-pass's column list for one object, or the catalog's when it has none.

        Nothing within one run changes the catalog's answer, so asking twice is a round trip
        for a list already in hand. An empty pre-pass list means a failed read or no pre-pass
        at all, so it is never reused as an answer.
        """

        entry = (inventory or {}).get(fqn)

        if entry is not None and entry.columns:
            return list(entry.columns)

        return self._adapter.introspect_columns(fqn)

    def _inferred_edges(
        self,
        fqn: str,
        declared: list[ForeignKeyMeta],
        inventory: dict[str, inference.TableInventory] | None,
    ) -> list[ForeignKeyMeta]:
        """Edges the naming rule derives for one table, stamped `inferred`."""

        if not self._conn.infer_relationships or not inventory or fqn not in inventory:
            return []

        edges = inference.infer_foreign_keys(inventory[fqn], inventory, declared)

        return [replace(edge, detection="inferred") for edge in edges]

    def _eligible_target(
        self,
        fqn: str,
        inventory: dict[str, inference.TableInventory] | None,
    ) -> bool | None:
        """Whether this table could ever be an inferred edge's target (SPEC 2.3.8).

        None when the pre-pass never ran (`infer_relationships: false`) - the same guard
        as `_inferred_edges`, so the two never disagree.
        """

        if not self._conn.infer_relationships or not inventory or fqn not in inventory:
            return None

        return inference.can_be_target(inventory[fqn])

    def _detect_columns(
        self,
        fqn: str,
        columns: list[ColumnMeta],
        base: dict[str, BaseStats],
        counts: TableCounts,
        fk_source_columns: frozenset[str],
        settings: TableSettings,
        scope: TableScope | None,
    ) -> dict[str, _ColumnDetection]:
        """Classify each column and run the SPEC 4.1.5 detections over it.

        Reads Phase A and the catalog only, so it runs before the expensive statistics and
        decides what they skip - under Phase A's own settings, since the adapter's
        pre-classification and this one must read a single `enumeration_threshold`.
        """

        out: dict[str, _ColumnDetection] = {}

        for col in columns:
            stats = base.get(col.name)

            if stats is None:
                continue

            # `supported` is reported, not re-derived (ARCHITECTURE.md 2, BaseStats).
            measured = stats.supported
            # Feeds `candidate_key` alone; `classify()` reads no ratio (SPEC 4.2).
            cardinality_ratio = (
                compute_cardinality_ratio(stats.cardinality, counts.rows_scanned)
                if measured
                else None
            )
            classification = classify(
                sql_type=col.sql_type,
                cardinality=stats.cardinality if measured else None,
                has_declared_fk=col.name in fk_source_columns,
                enumeration_threshold=settings.statistics.enumeration_threshold,
            )

            looks_like_value = None
            epoch_unit_value = None
            looks_like_sampled = None
            looks_like_matched = None
            samples: list[Any] = []

            # Sampling is what `looks_like` costs, so it is gated on the classifications
            # SPEC 4.1.5 names; `sensitivity` reads catalog metadata plus the sample if drawn.
            if classification in _LOOKS_LIKE_CLASSIFICATIONS:
                with _operation("sample_values"):
                    samples = self._adapter.sample_values(
                        fqn,
                        col.name,
                        settings.statistics.looks_like_sample_size,
                        scope,
                    )

                try:
                    match = detect_with_evidence(samples)
                    looks_like_value = match.pattern
                    epoch_unit_value = sample_epoch_unit(samples)
                except Exception as exc:  # noqa: BLE001 - contain to this column, not the table
                    _LOG.warning(
                        "looks_like/epoch_unit detection failed for %s.%s: %s",
                        fqn,
                        col.name,
                        exc,
                    )
                else:
                    # SPEC 4.1.5: `numeric_string` on a numeric-typed column restates the type
                    # it already carries, so it is withheld whatever the classification.
                    if looks_like_value == "numeric_string" and _matches(
                        base_type(col.sql_type),
                        _NUMERIC_TYPES,
                    ):
                        looks_like_value = None

                    # Evidence rides only beside a published verdict (SPEC 4.1.3).
                    if looks_like_value is not None:
                        looks_like_sampled = match.sampled
                        looks_like_matched = match.matched

            # Detection runs against the catalog's own spelling (SPEC 4.4.3), never the
            # lowercased map key - a token-boundary detector reads `firstName`.
            sensitivity = None

            if classification != "unsupported":
                try:
                    sensitivity = detect_sensitivity(
                        col.physical_name or col.name,
                        samples,
                        looks_like_value,
                    )
                except Exception as exc:  # noqa: BLE001 - contain to this column, not the table
                    _LOG.warning("sensitivity detection failed for %s.%s: %s", fqn, col.name, exc)
            # `candidate_key` rides the measured ratio alone (SPEC 4.2), independent of
            # `classification`; its exception marker is meaningful only in that same band.
            candidate_key = cardinality_ratio is not None and is_candidate_key(
                stats.cardinality,
                cardinality_ratio,
            )
            candidate_key_exception = (
                compute_candidate_key_exception(
                    stats.cardinality,
                    cardinality_ratio,
                    stats.cardinality_method,
                    counts.rows_scanned,
                    stats.null_count,
                )
                if candidate_key
                else None
            )
            inferred = Inferred(
                looks_like=looks_like_value,
                candidate_key=True if candidate_key else None,
                candidate_key_exception=candidate_key_exception,
                sensitivity=sensitivity,
                epoch_unit=epoch_unit_value,
                sampled=looks_like_sampled,
                matched=looks_like_matched,
            )

            if (
                inferred.looks_like is None
                and inferred.candidate_key is None
                and inferred.candidate_key_exception is None
                and inferred.sensitivity is None
                and inferred.epoch_unit is None
                and inferred.sampled is None
                and inferred.matched is None
            ):
                inferred = None

            redaction = (
                None
                if classification in _NO_CELL_VALUE_CLASSIFICATIONS
                else self._conn.redaction_for(f"{fqn}.{col.name}", sensitivity, looks_like_value)
            )
            out[col.name] = _ColumnDetection(
                classification=classification,
                inferred=inferred,
                redaction=redaction,
            )

        return out

    def _compute_grain(
        self,
        fqn: str,
        columns: list[ColumnMeta],
        base: dict[str, BaseStats],
        counts: TableCounts,
        detected: dict[str, _ColumnDetection],
        declared_keys: list[UniqueKeyMeta],
        scope: TableScope | None,
    ) -> Grain:
        """SPEC 2.2.12: every declared key, plus a bounded measured probe for the rest.

        Declared keys are catalog metadata, emitted at every arity in declaration order. The
        measured half issues no statement - `search_exhausted` stays None - under a scope, on
        an empty table, or where a column's `inferred.candidate_key` already answered.
        """

        keys = tuple(GrainKey(columns=uk.columns, detection="declared") for uk in declared_keys)

        if (
            scope is not None
            or counts.row_count == 0
            or counts.rows_scanned == 0
            or any(d.inferred is not None and d.inferred.candidate_key for d in detected.values())
        ):
            return Grain(keys=keys, search_exhausted=None)

        candidates, exhausted = _grain_search_candidates(columns, base, counts, declared_keys)
        found: tuple[tuple[str, str], ...] = ()

        if candidates:
            with _operation("probe_grain"):
                found = self._adapter.probe_grain(fqn, columns, counts, candidates, scope)

        keys = keys + tuple(GrainKey(columns=pair, detection="measured") for pair in found)

        return Grain(keys=keys, search_exhausted=exhausted)

    def _compute_dependencies(
        self,
        fqn: str,
        columns: list[ColumnMeta],
        base: dict[str, BaseStats],
        counts: TableCounts,
        detected: dict[str, _ColumnDetection],
        scope: TableScope | None,
    ) -> tuple[Dependency, ...]:
        """SPEC 2.2.13: pairwise functional dependencies over the scanned rows.

        Skipped under a `scope` - a dependency over a sample is not a dependency - and on an
        empty table, where every combination is vacuously functional. A pair below
        `_DEPENDENCY_STRENGTH_THRESHOLD` is independence, not a finding.
        """

        if scope is not None or counts.row_count == 0 or counts.rows_scanned == 0:
            return ()

        candidates = _dependency_candidates(columns, base, counts, detected)

        if not candidates:
            return ()

        with _operation("probe_dependencies"):
            strengths = self._adapter.probe_dependencies(
                fqn,
                columns,
                counts,
                base,
                candidates,
                scope,
            )

        return tuple(
            Dependency(determinant=a, dependent=b, strength=strength)
            for (a, b), strength in strengths.items()
            if strength >= _DEPENDENCY_STRENGTH_THRESHOLD
        )

    def _materialize_scope(self, fqn: str, scope: TableScope | None) -> TableScope | None:
        """The scope every statistics statement reads: one copied draw, or `scope` itself.

        Only a drawn fraction is copied, and only where the connection permits the write.
        A refusal degrades to a per-statement redraw, with a warning.
        """

        if scope is None or scope.sample is None or not self._conn.materialize_sample:
            return scope

        try:
            return self._adapter.materialize_scope(fqn, scope)
        except Exception as exc:  # noqa: BLE001 - degrade to a per-statement redraw
            _LOG.warning(
                "table %r: could not materialize its sample of %s (%s); each statistic for it "
                "is measured over its own draw of the rows",
                fqn,
                scope.sample,
                exc,
            )

            return scope

    def _release_scope(self, fqn: str, scope: TableScope | None) -> None:
        """Drop a materialized draw, never letting the cleanup mask the failure it follows."""

        if scope is None or scope.materialized is None:
            return

        try:
            self._adapter.release_scope(fqn, scope)
        except Exception as exc:  # noqa: BLE001 - cleanup must never mask the failure it follows
            _LOG.warning(
                "table %r: could not drop the materialized sample %r (%s); the session drops it",
                fqn,
                scope.materialized,
                exc,
            )

    def _write_relationships_artifacts(
        self,
        prints_root: Path,
        per_table_meta: dict[str, _PerTableContext],
        baseline_states: dict[str, diff_module.TableState] | None,
    ) -> None:
        for fqn, ctx in per_table_meta.items():
            artifact = _serialize_relationships(fqn, ctx, per_table_meta, baseline_states)
            write_atomic(ctx.tbl_dir, {"relationships.yaml": artifact})

    def _write_key_sketches(
        self,
        per_table_meta: dict[str, _PerTableContext],
        *,
        emitter: _ProgressEmitter | None = None,
    ) -> tuple[SketchFailure, ...]:
        """SPEC 2.2.14: sketch every join-key column and every likely edge-naming one.

        Patches `statistics.yaml` in place after phase 1, once the full edge set is known; a
        carried table is never touched, since a sketch needs a fresh read. An empty result means
        cardinality 0; the eligible work list is computed up front, so a table with nothing
        eligible consumes no bar slot.
        """

        emitter = emitter or _ProgressEmitter(None, self._conn.name)
        failures: list[SketchFailure] = []
        candidates: dict[str, set[str]] = {}

        for ctx in per_table_meta.values():
            for fk in ctx.relationships:
                if len(fk.column) == 1:
                    candidates.setdefault(ctx.fqn, set()).add(fk.column[0])

                if len(fk.target_column) == 1:
                    candidates.setdefault(fk.target_table, set()).add(fk.target_column[0])

        for ctx in per_table_meta.values():
            cols_payload = (
                ctx.statistics_payload.get("columns")
                if isinstance(ctx.statistics_payload, dict)
                else None
            )

            if not isinstance(cols_payload, dict):
                continue

            widened = _widened_sketch_candidates(
                cols_payload,
                ctx.unique_keys,
                self._conn.sketch_all_columns,
            )

            if widened:
                candidates.setdefault(ctx.fqn, set()).update(widened)

        eligible: dict[str, list[tuple[str, str, SketchKind]]] = {}

        for fqn, columns in candidates.items():
            ctx = per_table_meta.get(fqn)

            if ctx is None:
                continue  # target's own table wasn't re-extracted this run

            payload = ctx.statistics_payload

            if (
                not payload
                or isinstance(payload.get("scope"), dict)
                or payload.get("catalog_only") is True
            ):
                # No statistics, scoped (no reproducible sketch, SPEC 2.2.14), or nothing
                # was queried at all (SPEC 2.2.15) - a sketch needs a live read either way.
                continue

            cols_payload = payload.get("columns")

            if not isinstance(cols_payload, dict):
                continue

            sql_types = {c.name: c.sql_type for c in ctx.columns}
            eligible_columns: list[tuple[str, str, SketchKind]] = []

            for column in sorted(columns):
                col_payload = cols_payload.get(column)

                if not isinstance(col_payload, dict) or col_payload.get("redacted") is not None:
                    continue

                sql_type = sql_types.get(column)

                if sql_type is None:
                    continue

                kind = sketch_kind(sql_type)

                if kind is None:
                    continue

                eligible_columns.append((column, sql_type, kind))

            if eligible_columns:
                eligible[fqn] = eligible_columns

        sorted_fqns = sorted(eligible)
        table_total = len(sorted_fqns)

        if table_total == 0:
            # No eligible column anywhere - the pass never starts, so the renderer sees no bar
            # switch, no banner and no sketch event at all.
            return tuple(failures)

        emitter.sketch_phase("start", table_total)

        for table_index, fqn in enumerate(sorted_fqns, start=1):
            ctx = per_table_meta[fqn]
            payload = ctx.statistics_payload
            cols_payload = payload.get("columns")

            if not isinstance(cols_payload, dict):
                continue  # already proven true by the eligibility pass; narrows the type

            columns = eligible[fqn]
            column_total = len(columns)
            changed = False
            table_error: str | None = None
            started = time.monotonic()

            emitter.sketch_table("start", table_index, table_total, fqn)

            for column_index, (column, sql_type, kind) in enumerate(columns, start=1):
                emitter.sketch_column(
                    table_index,
                    table_total,
                    fqn,
                    column,
                    column_index,
                    column_total,
                )
                col_payload = cols_payload[column]

                try:
                    with _operation("compute_key_sketch"):
                        hashes = self._adapter.compute_key_sketch(
                            fqn,
                            column,
                            sql_type,
                            kind,
                            SKETCH_K,
                        )
                except Exception as exc:  # noqa: BLE001 - run-all-then-report; this column only
                    cause = exc.cause if isinstance(exc, _OperationFailed) else exc
                    error_text = _one_line(cause)
                    failures.append(SketchFailure(table=fqn, column=column, error=error_text))
                    table_error = table_error or error_text
                    continue

                if not hashes and col_payload.get("cardinality") != 0:
                    continue  # adapter declined (e.g. an unreadable column) - no honest answer

                col_payload["sketch"] = {
                    "method": SKETCH_METHOD,
                    "values": pack_sketch(list(hashes)),
                }
                changed = True

            elapsed_ms = int((time.monotonic() - started) * 1000)
            emitter.sketch_table(
                "failed" if table_error is not None else "done",
                table_index,
                table_total,
                fqn,
                elapsed_ms=elapsed_ms,
                error=table_error,
            )

            if changed:
                ctx.statistics_yaml = _dump_yaml(payload)
                write_atomic(ctx.tbl_dir, {"statistics.yaml": ctx.statistics_yaml})

        emitter.sketch_phase("done", table_total)

        return tuple(failures)

    def _write_manifest_artifacts(
        self,
        prints_root: Path,
        outcome: _ExtractionOutcome,
        baseline_manifest: dict[str, Any] | None,
        generated_at: str,
    ) -> None:
        """Write the connection-root manifest.yaml, diff.yaml and reading.md atomically.

        The manifest is left in place when this run reproduced it exactly, so a no-op run does
        not land a commit under a fresh `generated_at`. `reading.md` is always included.
        """

        entries = _build_manifest_entries(outcome, baseline_manifest, self._conn)
        manifest_dict = build_manifest(
            connection_name=self._conn.name,
            adapter_kind=self._conn.adapter,
            entries=entries,
            generated_at=generated_at,
            statistics_params=_statistics_params_dict(self._conn.statistics),
            selectors=diff_module.DiffSelectors(include=outcome.include, exclude=outcome.exclude),
            redaction_rules_configured=len(self._conn.redact),
            default_collation=outcome.default_collation,
            has_manifest_annotations=(prints_root / MANIFEST_ANNOTATIONS_FILENAME).is_file(),
        )
        artifacts: dict[str, str | bytes] = {
            "diff.yaml": _dump_yaml(outcome.diff_dict),
            READING_GUIDE_FILENAME: READING_GUIDE_TEXT,
        }

        if not _manifest_unchanged(manifest_dict, baseline_manifest):
            artifacts["manifest.yaml"] = _dump_yaml(manifest_dict)

        prints_root.mkdir(parents=True, exist_ok=True)
        write_atomic(prints_root, artifacts)


# Auxiliary dataclasses.


@dataclass
class _ExtractionOutcome:
    """Result of running the shared extract+graph+diff pipeline.

    `not_attempted` non-zero means fail-fast left matched tables unreached, so no
    connection-level artifact may be written. `carried_*` are baseline tables the manifest
    inherits and `dropped_carried` the subset it omits; the rest record what this run judged
    each table under (SPEC 2.2.2, 2.5).
    """

    per_table_results: list[TableResult]
    per_table_meta: dict[str, _PerTableContext]
    diff_dict: dict[str, Any]
    not_attempted: int = 0
    matched_fqns: tuple[str, ...] = ()
    carried_matched: tuple[str, ...] = ()
    carried_out_of_scope: tuple[str, ...] = ()
    dropped_carried: tuple[str, ...] = ()
    resolved_thresholds: dict[str, int] = field(default_factory=dict)
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    default_collation: str = ""
    sketch_failures: tuple[SketchFailure, ...] = ()


@dataclass
class _ColumnDetection:
    """What the engine works out about one column before Phase B runs.

    Derived from Phase A's counts, the column's type and a value sample alone, which is what
    makes it available in time to tell Phase B what to skip.
    """

    classification: Classification
    inferred: Inferred | None
    redaction: str | None = None


@dataclass
class _EnrichedColumnStats:
    """Adapter stats plus the spec-derived classification and freshness the engine applies.

    `freshness` is engine-derived from `stats.range.max`; `values_coverage_method` is stamped
    after assembly, not at construction.
    """

    stats: ColumnStats
    classification: Classification
    inferred: Inferred | None
    redaction: str | None = None
    freshness: Freshness | None = None
    physical_name: str | None = None
    collation: str | None = None
    values_coverage_method: str | None = None


@dataclass
class _PerTableContext:
    """Everything extracted for one table, carried from extraction to write.

    `statistics_payload` is `statistics_yaml` loaded back, so redaction, the field matrix and
    numeric rendering are already applied; a view's is catalog-only. `unique_keys` holds every
    declared group, single- and multi-column, and is empty on a view.
    """

    fqn: str
    type: TableType
    namespace_path: tuple[str, ...]
    columns: list[ColumnMeta]
    relationships: list[ForeignKeyMeta]
    indexes: list[IndexMeta]
    comments: Any
    ddl: str
    statistics_yaml: str | None
    statistics_payload: dict[str, Any]
    profiled_at: str
    row_count: int | None
    row_count_estimate: int | None = None
    max_age_days: int | None = None
    rows_scanned: int | None = None
    scope: TableScope | None = None
    eligible_target: bool | None = None
    has_description: bool = False
    has_statistics_annotations: bool = False
    has_relationships_annotations: bool = False
    tbl_dir: Path = field(default=Path("/dev/null"))
    referenced_by: list[Any] = field(default_factory=list)
    unique_keys: tuple[UniqueKeyMeta, ...] = field(default_factory=tuple)


def _widened_sketch_candidates(
    columns_payload: dict[str, Any],
    unique_keys: tuple[UniqueKeyMeta, ...],
    sketch_all_columns: bool,
) -> set[str]:
    """SPEC 2.2.14's MAY-set: declared-unique, exhaustive-sized, or a measured candidate key.

    `sketch_all_columns` replaces the three conditions with every column. Type and exclusion
    filtering (redacted, unsketchable, scope, catalog-only) happens in the caller, not here.
    """

    if sketch_all_columns:
        return set(columns_payload)

    out = {uk.columns[0] for uk in unique_keys if len(uk.columns) == 1} & set(columns_payload)

    for name, col_payload in columns_payload.items():
        if not isinstance(col_payload, dict):
            continue

        cardinality = col_payload.get("cardinality")

        if isinstance(cardinality, int) and cardinality <= SKETCH_K:
            out.add(name)
            continue

        inferred = col_payload.get("inferred")

        if isinstance(inferred, dict) and inferred.get("candidate_key") is True:
            out.add(name)

    return out


@dataclass
class _DiffResult:
    """One connection's diff outcome: the summary plus whether its shape moved."""

    summary: DiffSummary
    has_schema_changes: bool


class _ProgressEmitter:
    """Engine-owned progress seam: builds ProgressEvents and shields the run.

    A throwing user callback is logged and swallowed so emission can never abort extraction;
    with no callback every method is a cheap no-op.
    """

    def __init__(self, on_progress: ProgressCallback | None, connection: str) -> None:
        self._cb = on_progress
        self._connection = connection

    @property
    def enabled(self) -> bool:
        return self._cb is not None

    def connecting(self, status: ProgressStatus) -> None:
        """Bracket `adapter.connect()` - the run's first wait, and its shortest."""

        self._emit("connecting", status, 0, 0, None)

    def listing(self, status: ProgressStatus, total: int = 0) -> None:
        """Bracket `list_tables()`. `total` is unknown at `start` and real at `done`."""

        self._emit("listing", status, 0, total, None)

    def inventory_phase(self, status: ProgressStatus, total: int) -> None:
        """Bracket the whole relationship-inference pre-pass over `total` objects."""

        self._emit("inventory", status, 0, total, None)

    def inventory_tick(self, index: int, total: int, fqn: str | None = None) -> None:
        """One object read within the pre-pass - the signal a wide connection needs."""

        self._emit("inventory", "start", index, total, fqn)

    def table_start(self, index: int, total: int, fqn: str) -> None:
        self._emit("extract", "start", index, total, fqn)

    def table_phase(self, index: int, total: int, fqn: str, phase: ProgressPhase) -> None:
        self._emit(phase, "start", index, total, fqn)

    def table_done(
        self,
        index: int,
        total: int,
        result: TableResult,
        ctx: _PerTableContext | None,
    ) -> None:
        phase: ProgressPhase = "write" if result.status == "ok" else "extract"
        self._emit(
            phase,
            _TERMINAL_STATUS[result.status],
            index,
            total,
            result.fqn,
            elapsed_ms=result.elapsed_ms,
            row_count=ctx.row_count if ctx is not None else None,
            error=result.error,
        )

    def column_hook(self, index: int, total: int, fqn: str) -> ColumnProgress:
        """Return a per-column callback the adapter drives during the statistics phase."""

        def _hook(column_index: int, column_total: int, name: str) -> None:
            self._emit(
                "statistics",
                "start",
                index,
                total,
                fqn,
                column=name,
                column_index=column_index,
                column_total=column_total,
            )

        return _hook

    def finalizing(self, status: ProgressStatus, total: int) -> None:
        self._emit("finalizing", status, total, total, None)

    def sketch_phase(self, status: ProgressStatus, total: int) -> None:
        self._emit("sketch", status, 0, total, None)

    def sketch_table(
        self,
        status: ProgressStatus,
        index: int,
        total: int,
        fqn: str,
        *,
        elapsed_ms: int | None = None,
        error: str | None = None,
    ) -> None:
        self._emit("sketch", status, index, total, fqn, elapsed_ms=elapsed_ms, error=error)

    def sketch_column(
        self,
        index: int,
        total: int,
        fqn: str,
        column: str,
        column_index: int,
        column_total: int,
    ) -> None:
        """`index`/`total` stay table-scoped."""

        self._emit(
            "sketch",
            "start",
            index,
            total,
            fqn,
            column=column,
            column_index=column_index,
            column_total=column_total,
        )

    def _emit(
        self,
        phase: ProgressPhase,
        status: ProgressStatus,
        index: int,
        total: int,
        fqn: str | None,
        *,
        column: str | None = None,
        column_index: int | None = None,
        column_total: int | None = None,
        elapsed_ms: int | None = None,
        row_count: int | None = None,
        error: str | None = None,
    ) -> None:
        if self._cb is None:
            return

        event = ProgressEvent(
            connection=self._connection,
            phase=phase,
            status=status,
            index=index,
            total=total,
            fqn=fqn,
            column=column,
            column_index=column_index,
            column_total=column_total,
            elapsed_ms=elapsed_ms,
            row_count=row_count,
            error=error,
        )

        try:
            self._cb(event)
        except Exception as exc:  # noqa: BLE001 - a caller's callback must not take down the run
            _LOG.warning("progress callback raised for %r: %s", fqn, exc)


# Helpers.


class _OperationFailed(RuntimeError):
    """Carries which adapter operation raised so the capture site can name it."""

    def __init__(self, operation: str, cause: BaseException) -> None:
        super().__init__(str(cause))
        self.operation = operation
        self.cause = cause


@contextmanager
def _operation(name: str) -> Iterator[None]:
    """Tag a failure raised inside with the adapter operation that produced it.

    Covers pure-Python failures (identifier rejection, DDL normalization) with no statement of
    their own. Also sets the statement-trace phase exec_query's trace record reads - the same
    operation name, not a second vocabulary.
    """

    phase_token = trace_context.phase.set(name)

    try:
        yield
    except _OperationFailed:
        raise
    except Exception as exc:
        raise _OperationFailed(name, exc) from exc
    finally:
        trace_context.phase.reset(phase_token)


def _log_table_result(
    result: TableResult,
    ctx: _PerTableContext | None,
    matched_rules: tuple[str, ...],
) -> None:
    """Run-log record for one table: outcome, rules, counts, elapsed, traceback on failure."""

    _LOG.info(
        "table %r: outcome=%s rules=%s row_count=%s rows_scanned=%s elapsed_ms=%d",
        result.fqn,
        result.status,
        ",".join(matched_rules) or "-",
        ctx.row_count if ctx is not None else None,
        ctx.rows_scanned if ctx is not None else None,
        result.elapsed_ms,
    )

    if result.status == "failed" and result.error_traceback:
        _LOG.info("table %r failed:\n%s", result.fqn, result.error_traceback.rstrip())


def _error_fields(exc: BaseException) -> dict[str, str | None]:
    """Build the one-line cause, the detail block, and the traceback text."""

    operation: str | None = None
    cause: BaseException = exc

    if isinstance(exc, _OperationFailed):
        operation = exc.operation
        cause = exc.cause

    return {
        "error": _one_line(cause),
        "error_operation": operation,
        "error_detail": cause.detail() if isinstance(cause, QueryFailed) else None,
        "error_traceback": "".join(traceback.format_exception(exc)),
    }


def _one_line(cause: BaseException) -> str:
    """`<ExcType>: <message>` - QueryFailed already renders exactly that form."""

    if isinstance(cause, QueryFailed):
        return str(cause)

    return f"{type(cause).__name__}: {cause}"


def _utc_iso_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _is_fresh(
    baseline_manifest: dict[str, Any] | None,
    fqn: str,
    max_age_days: int,
    generated_at: str,
) -> bool:
    if not baseline_manifest:
        return False

    entry = (baseline_manifest.get("tables") or {}).get(fqn)

    if not isinstance(entry, dict) or "profiled_at" not in entry:
        return False

    try:
        prior = datetime.fromisoformat(entry["profiled_at"])
        now = datetime.fromisoformat(generated_at)
    except (ValueError, AttributeError):
        return False

    age_days = (now - prior).total_seconds() / 86400.0

    return age_days < max_age_days


def _suppressed_columns(detected: dict[str, _ColumnDetection]) -> frozenset[str]:
    """The columns whose value enumeration Phase B must not issue.

    Only `text`: SPEC 4.1.5 runs detection on `categorical` too, but its matrix row requires
    the list unconditionally - the format's exemption covers `text` alone.
    """

    return frozenset(
        name
        for name, detection in detected.items()
        if detection.classification == "text"
        and detection.inferred is not None
        and detection.inferred.looks_like == _PROSE
    )


def _assemble_stats(
    fqn: str,
    columns: list[ColumnMeta],
    stats: dict[str, ColumnStats],
    detected: dict[str, _ColumnDetection],
    suppressed: frozenset[str],
    generated_at: str,
) -> dict[str, _EnrichedColumnStats]:
    """Join Phase B's statistics to the detections made before it ran, deriving freshness."""

    out: dict[str, _EnrichedColumnStats] = {}

    for col in columns:
        column_stats = stats.get(col.name)
        detection = detected.get(col.name)

        if column_stats is None or detection is None:
            continue

        if col.name in suppressed:
            # Phase B was asked to skip the enumeration; clearing the fields here too makes
            # the emitted shape follow from the request, not from an adapter honouring it.
            column_stats = replace(
                column_stats,
                values=None,
                values_coverage=None,
                distribution=None,
            )
        else:
            column_stats = _fill_empty_value_shape(column_stats, detection.classification)

        inferred = detection.inferred

        # The bounds rule reads Phase B's own range, so it cannot run in `_detect_columns`.
        # `drop` removes `range` only at serialization, so a dropped column still reports it.
        if detection.classification == "numeric" and column_stats.range is not None:
            try:
                epoch_unit_value = bounds_epoch_unit(column_stats.range.min, column_stats.range.max)
            except Exception as exc:  # noqa: BLE001 - contain to this column, not the table
                _LOG.warning("bounds_epoch_unit failed for %s.%s: %s", fqn, col.name, exc)
                epoch_unit_value = None

            if epoch_unit_value is not None:
                inferred = (
                    replace(inferred, epoch_unit=epoch_unit_value)
                    if inferred is not None
                    else Inferred(epoch_unit=epoch_unit_value)
                )

        freshness = None

        if detection.classification == "temporal" and column_stats.range is not None:
            freshness = _derive_freshness(column_stats.range.max, generated_at)

        out[col.name] = _EnrichedColumnStats(
            stats=column_stats,
            classification=detection.classification,
            inferred=inferred,
            redaction=detection.redaction,
            freshness=freshness,
            physical_name=col.physical_name,
            collation=col.collation,
        )

    return out


def _derive_freshness(range_max: Any, profiled_at: str) -> Freshness:
    """SPEC 2.2.4: `max_age_days` from the measured maximum and the run's own instant."""

    days = derive_max_age_days(range_max, profiled_at)

    return Freshness(max_age_days=days, classification=freshness_classification(days))


def _stamp_values_coverage_method(
    fqn: str,
    rows_scanned: int,
    enriched: dict[str, _EnrichedColumnStats],
) -> None:
    """Stamp `values_coverage_method` (SPEC 2.2.4) on every column carrying a value list.

    `measured` when the list is exhaustive and its counts do not overrun the rows scanned,
    `bounded` when they do, absent for a truncated list - there is nothing to measure against.
    Mirrors `null_patterns.coverage_method`'s two words.
    """

    for name, e in enriched.items():
        if e.stats.values is None:
            continue

        listed = sum(v.count for v in e.stats.values)
        non_null = rows_scanned - e.stats.null_count

        if is_incoherent(listed, non_null):
            e.values_coverage_method = "bounded"
            _LOG.warning(
                "column %r of table %r: listed value counts (%d) exceed the "
                "non-null rows scanned (%d) - values_coverage was bounded rather than published raw",
                name,
                fqn,
                listed,
                non_null,
            )
        elif e.stats.values_coverage == 1.0:
            e.values_coverage_method = "measured"


def _fill_empty_value_shape(stats: ColumnStats, classification: Classification) -> ColumnStats:
    """Fill zero-cardinality columns with the EMPTY form of their value fields.

    The conformance matrix requires a value list from categorical / foreign_key_candidate /
    boolean (SPEC 2.2.7). Only None fields are filled.
    """

    if stats.cardinality != 0:
        return stats

    updates: dict[str, Any] = {}

    if classification in _VALUE_LIST_CLASSIFICATIONS:
        if stats.values is None:
            updates["values"] = ()

        if stats.values_coverage is None:
            # Nothing to list is everything there is to list.
            updates["values_coverage"] = 1.0

    if classification in ("categorical", "foreign_key_candidate") and stats.distribution is None:
        updates["distribution"] = "uniform"

    return replace(stats, **updates) if updates else stats


def _grain_search_candidates(
    columns: list[ColumnMeta],
    base: dict[str, BaseStats],
    counts: TableCounts,
    declared_keys: list[UniqueKeyMeta],
) -> tuple[tuple[tuple[str, str], ...], bool]:
    """The measured probe's candidate pairs, arithmetic-pruned and capped. See SPEC 2.2.12.

    Null-free columns only (`COUNT(DISTINCT a, b)` diverges on nulls across dialects), pruned
    by `cardinality(a) * cardinality(b) >= row_count`, ordered declared-unique first then by
    descending cardinality. `exhausted` is true when the whole pruned space fit under the cap.
    """

    declared_pairs = {tuple(sorted(uk.columns)) for uk in declared_keys if len(uk.columns) == 2}
    declared_singles = {uk.columns[0] for uk in declared_keys if len(uk.columns) == 1}

    null_free = [
        col.name
        for col in columns
        if (stats := base.get(col.name)) is not None and stats.supported and stats.null_count == 0
    ]
    ordered = sorted(
        null_free,
        key=lambda name: (name not in declared_singles, -base[name].cardinality),
    )

    pruned = [
        pair
        for pair in itertools.combinations(ordered, 2)
        if tuple(sorted(pair)) not in declared_pairs
        and base[pair[0]].cardinality * base[pair[1]].cardinality >= counts.row_count
    ]

    return tuple(pruned[:_GRAIN_SEARCH_CAP]), len(pruned) <= _GRAIN_SEARCH_CAP


def _name_adjacent(a: str, b: str) -> bool:
    """Whether one column's underscore-token sequence is a strict prefix of the other's."""

    tokens_a, tokens_b = a.split("_"), b.split("_")

    if len(tokens_a) == len(tokens_b):
        return False

    shorter, longer = (
        (tokens_a, tokens_b) if len(tokens_a) < len(tokens_b) else (tokens_b, tokens_a)
    )

    return longer[: len(shorter)] == shorter


def _dependency_candidates(
    columns: list[ColumnMeta],
    base: dict[str, BaseStats],
    counts: TableCounts,
    detected: dict[str, _ColumnDetection],
) -> tuple[tuple[str, str], ...]:
    """The measured probe's candidate pairs, arithmetic-pruned and capped. See SPEC 2.2.13.

    Null-free columns only, as in `grain`'s search. A pair qualifies when both columns
    classify categorical/boolean or their names are adjacent, and only orientations with
    `cardinality(determinant) >= cardinality(dependent)` are tested - both on a tie. A
    near-unique determinant or constant dependent is vacuous and excluded.
    """

    eligible = [
        col.name
        for col in columns
        if (stats := base.get(col.name)) is not None and stats.supported and stats.null_count == 0
    ]
    classified = {name: detected[name].classification for name in eligible if name in detected}

    def qualifies(a: str, b: str) -> bool:
        low_cardinality = (
            classified.get(a) in _LOW_CARDINALITY_CLASSIFICATIONS
            and classified.get(b) in _LOW_CARDINALITY_CLASSIFICATIONS
        )

        return low_cardinality or _name_adjacent(a, b)

    def viable(determinant: str, dependent: str) -> bool:
        if base[dependent].cardinality <= 1:
            return False

        ratio = compute_cardinality_ratio(base[determinant].cardinality, counts.rows_scanned)

        return not is_candidate_key(base[determinant].cardinality, ratio)

    pairs: list[tuple[str, str]] = []

    for x, y in itertools.combinations(eligible, 2):
        if not qualifies(x, y):
            continue

        card_x, card_y = base[x].cardinality, base[y].cardinality

        if card_x >= card_y and viable(x, y):
            pairs.append((x, y))

        if card_y >= card_x and viable(y, x):
            pairs.append((y, x))

    ordered = sorted(
        pairs,
        key=lambda pair: (
            not _name_adjacent(*pair),
            base[pair[0]].cardinality * base[pair[1]].cardinality,
        ),
    )

    return tuple(ordered[:_DEPENDENCY_SEARCH_CAP])


def _table_scope(settings: TableSettings) -> TableScope | None:
    """Row-level narrowing in force for one table, or None for a full scan."""

    scope = TableScope(sample=settings.sample, filter=settings.filter)

    return scope if scope.narrows else None


def _serialize_statistics(
    fqn: str,
    table_type: str,
    profiled_at: str,
    counts: TableCounts,
    enriched: dict[str, _EnrichedColumnStats],
    scope: TableScope | None = None,
    salt: str = "",
    null_patterns: NullPatterns | None = None,
    physical_layout: PhysicalLayout | None = None,
    default_collation: str = "",
    grain: Grain | None = None,
    dependencies: tuple[Dependency, ...] = (),
) -> str:
    columns_payload: dict[str, Any] = {}
    narrows = scope is not None and scope.narrows
    # Only a key with a recovered base column can be flagged; an expression key has none.
    physical_layout_columns = (
        {key.column for key in physical_layout.keys if key.column is not None}
        if physical_layout is not None
        else frozenset()
    )

    for name, e in enriched.items():
        col_dict: dict[str, Any] = {
            "sql_type": e.stats.sql_type,
            "nullable": e.stats.nullable,
            "null_count": e.stats.null_count,
            "null_rate": e.stats.null_rate,
            "classification": e.classification,
        }

        # Omitted where it coincides with the map key (SPEC 2.2.4).
        if e.physical_name is not None and e.physical_name != name:
            col_dict["physical_name"] = e.physical_name

        # Omitted where it coincides with the connection default (SPEC 2.2.2).
        if e.collation is not None and e.collation != default_collation:
            col_dict["collation"] = e.collation

        if e.classification != "unsupported":
            col_dict["cardinality"] = e.stats.cardinality
            col_dict["cardinality_ratio"] = e.stats.cardinality_ratio
            col_dict["cardinality_method"] = e.stats.cardinality_method

        # SPEC 2.2.8: every ratio above is relative to this population, so one column
        # block recomputes without the file head. Absent when rows_scanned == row_count.
        if narrows:
            col_dict["rows_scanned"] = counts.rows_scanned

        if name in physical_layout_columns:
            col_dict["physical_layout_key"] = True

        for field_name, value in _emitted_extras(e, salt):
            col_dict[field_name] = value

        _drop_forbidden_fields(fqn, name, e.classification, col_dict)
        columns_payload[name] = col_dict

    payload: dict[str, Any] = {
        "format_version": FORMAT_VERSION,
        "table": fqn,
        "type": table_type,
        "profiled_at": profiled_at,
        "row_count": counts.row_count,
        # Stamped as the adapter reported it: a narrowed read normally takes the catalog
        # estimate, but a table the catalog cannot size is counted instead.
        "row_count_method": counts.row_count_method,
    }

    if narrows and scope is not None:
        block: dict[str, Any] = {"rows_scanned": counts.rows_scanned}

        if scope.sample is not None:
            block["sample"] = scope.sample

        if scope.filter:
            block["filter"] = scope.filter

        payload["scope"] = block

    # SPEC 2.2.10: absent means no column carries a null, so it is never emitted empty
    # to stand for "measured, nothing found".
    if null_patterns is not None:
        null_patterns_block: dict[str, Any] = {"coverage": null_patterns.coverage}

        if null_patterns.coverage_method is not None:
            null_patterns_block["coverage_method"] = null_patterns.coverage_method

        null_patterns_block["patterns"] = [
            {"columns": list(pattern.columns), "count": pattern.count}
            for pattern in null_patterns.patterns
        ]
        payload["null_patterns"] = null_patterns_block

    # Absent means "not clustered", never "not checked" - every adapter answers it.
    if physical_layout is not None:
        payload["physical_layout"] = {
            "mechanism": physical_layout.mechanism,
            "keys": [
                {"expression": key.expression, "column": key.column}
                if key.column is not None
                else {"expression": key.expression}
                for key in physical_layout.keys
            ],
        }

    # Declared keys are catalog-cheap, so `keys: []` states "nothing declared, nothing
    # measured" rather than leaving SPEC 2.2.12's question unanswered.
    if grain is not None:
        grain_block: dict[str, Any] = {
            "keys": [
                {"columns": list(key.columns), "detection": key.detection} for key in grain.keys
            ],
        }

        if grain.search_exhausted is not None:
            grain_block["search"] = {"exhausted": grain.search_exhausted}

        payload["grain"] = grain_block

    # Always emitted, `[]` for nothing - the same "answered, not skipped" convention as `grain`.
    payload["dependencies"] = [
        {"determinant": d.determinant, "dependent": d.dependent, "strength": d.strength}
        for d in dependencies
    ]

    payload["columns"] = columns_payload

    return _dump_yaml(payload)


def _serialize_catalog_only_statistics(
    fqn: str,
    table_type: str,
    profiled_at: str,
    columns: list[ColumnMeta],
    fk_source_columns: frozenset[str],
    enumeration_threshold: int,
) -> str:
    """The statistics artifact for an object nothing was queried for (SPEC 2.2.15).

    Every field a catalog/DDL read already supplies and nothing else; `classify()` runs with
    `catalog_only=True`, which changes only its unmatched-type fallthrough (SPEC 3.3).
    """

    columns_payload: dict[str, Any] = {}

    for col in columns:
        classification = classify(
            sql_type=col.sql_type,
            cardinality=None,
            has_declared_fk=col.name in fk_source_columns,
            enumeration_threshold=enumeration_threshold,
            catalog_only=True,
        )
        col_dict: dict[str, Any] = {
            "sql_type": col.sql_type,
            "nullable": col.nullable,
            "classification": classification,
        }

        if col.physical_name is not None and col.physical_name != col.name:
            col_dict["physical_name"] = col.physical_name

        if col.collation is not None:
            col_dict["collation"] = col.collation

        columns_payload[col.name] = col_dict

    payload: dict[str, Any] = {
        "format_version": FORMAT_VERSION,
        "table": fqn,
        "type": table_type,
        "profiled_at": profiled_at,
        "catalog_only": True,
        "grain": {"keys": []},
        "columns": columns_payload,
    }

    return _dump_yaml(payload)


def _drop_forbidden_fields(
    fqn: str,
    column: str,
    classification: Classification,
    col_dict: dict[str, Any],
) -> None:
    """Refuse any field SPEC 2.2.3 forbids for `classification`; warn about the drop.

    A backstop - every path building `col_dict` keeps classification and computed fields in
    step, so a drop means they disagreed and the reader is told rather than left guessing.
    """

    forbidden = FORBIDDEN_FIELDS.get(classification, frozenset())
    dropped = sorted(name for name in col_dict if name in forbidden)

    for field_name in dropped:
        del col_dict[field_name]

    if dropped:
        _LOG.warning(
            "table %r, column %r: classification %r forbids %s; suppressed on the way to the file",
            fqn,
            column,
            classification,
            ", ".join(dropped),
        )


def _emitted_extras(e: _EnrichedColumnStats, salt: str = ""):
    """Every value-bearing field for one column, redacted where a rule covers it.

    The value list, range bounds and percentiles are the cell values a primitive acts on;
    counts, ratios, coverage, cardinality and distribution pass through. `redacted` is yielded
    only when the column carries one of those three - declaring a redaction over nothing to
    redact is a claim the artifact cannot support.
    """

    s = e.stats

    if e.redaction is not None and (
        s.values is not None or s.range is not None or s.percentiles is not None
    ):
        yield "redacted", e.redaction

    if e.inferred is not None:
        inf: dict[str, Any] = {}

        if e.inferred.looks_like is not None:
            inf["looks_like"] = e.inferred.looks_like

        if e.inferred.sampled is not None:
            inf["sampled"] = e.inferred.sampled

        if e.inferred.matched is not None:
            inf["matched"] = e.inferred.matched

        if e.inferred.candidate_key:
            inf["candidate_key"] = True

        if e.inferred.candidate_key_exception is not None:
            inf["candidate_key_exception"] = e.inferred.candidate_key_exception

        if e.inferred.sensitivity is not None:
            inf["sensitivity"] = e.inferred.sensitivity

        if e.inferred.epoch_unit is not None:
            inf["epoch_unit"] = e.inferred.epoch_unit

        if inf:
            yield "inferred", inf

    if s.values is not None:
        yield "values", [_redacted_entry(v, e.redaction, salt) for v in s.values]

    if s.values_coverage is not None:
        yield "values_coverage", s.values_coverage

        if e.values_coverage_method is not None:
            yield "values_coverage_method", e.values_coverage_method

    if s.distribution is not None:
        yield "distribution", s.distribution

    if s.frequencies is not None:
        yield (
            "frequencies",
            {
                "top": s.frequencies.top,
                "bottom": s.frequencies.bottom,
                "listed": s.frequencies.listed,
                "total": s.frequencies.total,
            },
        )

    # A bound is nothing but a literal, so `drop` removes the fields rather than leaving a
    # placeholder - the one primitive where redacting and omitting the bounds coincide.
    if s.range is not None and e.redaction != "drop":
        rng_dict: dict[str, Any] = {
            "min": _redacted_scalar(s.range.min, e.redaction, salt),
            "max": _redacted_scalar(s.range.max, e.redaction, salt),
        }

        if s.range.span_days is not None:
            rng_dict["span_days"] = _coarsened_day_count(s.range.span_days, e.redaction)
        yield "range", rng_dict

    if s.percentiles is not None and e.redaction != "drop":
        yield (
            "percentiles",
            {k: _redacted_scalar(v, e.redaction, salt) for k, v in s.percentiles.items()},
        )

    if e.freshness is not None:
        yield (
            "freshness",
            {
                "max_age_days": _coarsened_day_count(e.freshness.max_age_days, e.redaction),
                "classification": e.freshness.classification,
            },
        )

    # A name pointing at a dropped bound fails conformance's "names an emitted field" check
    # (SPEC 2.2.4), so this follows `range`/`percentiles` under `drop` too.
    if s.unrepresentable and e.redaction != "drop":
        yield "unrepresentable", list(s.unrepresentable)


def _redacted_entry(value_count: Any, primitive: str | None, salt: str) -> dict[str, Any]:
    """One `values` entry, with its literal replaced, dropped, or left alone.

    Under `drop` the `value` key is absent and the count remains - how many rows shared some
    value, without saying which.
    """

    if primitive is None:
        return {"value": value_count.value, "count": value_count.count}

    if primitive == "drop":
        return {"count": value_count.count}

    # config validated the primitive; it is a plain string because `config` sits below `spec`.
    return {
        "value": redact_value(value_count.value, cast(Primitive, primitive), salt),
        "count": value_count.count,
    }


def _redacted_scalar(value: Any, primitive: str | None, salt: str) -> Any:
    if primitive is None or value is None:
        return value

    return redact_value(value, cast(Primitive, primitive), salt)


def _coarsened_day_count(days: int, primitive: str | None) -> int:
    """A derived day count, floored to `REDACTED_DAY_COUNT_GRANULARITY` under any marker.

    Every primitive coarsens, `drop` included - it removes `range` but leaves `freshness`
    (SPEC 2.2.3), and an unmarked derived integer reconstructs the withheld bound.
    """

    return days if primitive is None else coarsen_day_count(days)


def _serialize_relationships(
    fqn: str,
    ctx: _PerTableContext,
    per_table_meta: dict[str, _PerTableContext],
    baseline_states: dict[str, diff_module.TableState] | None,
) -> str:
    # `constraint_name` is OPTIONAL per SPEC 2.3.2 and is omitted rather than nulled: an
    # inferred edge has no constraint to name, and a null there is a type error.
    refers_to_payload = [
        _without_none(
            {
                "column": list(fk.column),
                "target_table": fk.target_table,
                "target_column": list(fk.target_column),
                **_fk_action_fields(fk.detection, fk.on_delete, fk.on_update),
                "detection": fk.detection,
                "constraint_name": fk.constraint_name,
                "observed": _compute_observed(
                    fqn,
                    fk.column,
                    fk.target_table,
                    fk.target_column,
                    per_table_meta,
                    baseline_states,
                ),
            },
        )
        for fk in ctx.relationships
    ]

    referenced_by_payload = [
        _without_none(
            {
                "column": list(e.column),
                "referencer_table": e.referencer_table,
                "referencer_column": list(e.referencer_column),
                **_fk_action_fields(e.detection, e.on_delete, e.on_update),
                "detection": e.detection,
                "constraint_name": e.constraint_name,
                "observed": _compute_observed(
                    e.referencer_table,
                    e.referencer_column,
                    fqn,
                    e.column,
                    per_table_meta,
                    baseline_states,
                ),
            },
        )
        for e in ctx.referenced_by
    ]

    payload: dict[str, Any] = {
        "format_version": FORMAT_VERSION,
        "table": fqn,
        "profiled_at": ctx.profiled_at,
    }

    if ctx.eligible_target is not None:
        payload["eligible_target"] = ctx.eligible_target

    payload["refers_to"] = refers_to_payload
    payload["referenced_by"] = referenced_by_payload

    return _dump_yaml(payload)


def _fk_action_fields(detection: str, on_delete: str, on_update: str) -> dict[str, str]:
    """SPEC 2.3.8: an inferred edge carries no referential action; only a declared one does.

    Emitting `NO ACTION` would dress a guess in the clothing of a real constraint.
    """

    if detection == "inferred":
        return {}

    return {"on_delete": on_delete, "on_update": on_update}


def _without_none(entry: dict[str, Any]) -> dict[str, Any]:
    """Drop keys whose value is absent, so an optional field is omitted not nulled."""

    return {k: v for k, v in entry.items() if v is not None}


@dataclass(frozen=True)
class _ColumnSnapshot:
    """One column's numbers as `_compute_observed` needs them, whichever source they came from.

    `sample` and `sketch` are `None` from a carried table, so `containment` and a measured
    `target_coverage` only ever fire between two columns this run re-extracted.
    """

    row_count: int | None
    scoped: bool
    sample: float | None
    cardinality: int | None
    cardinality_method: str | None
    top_count: int | None
    sketch: tuple[int, ...] | None


def _column_snapshot(
    fqn: str,
    column: str,
    per_table_meta: dict[str, _PerTableContext],
    baseline_states: dict[str, diff_module.TableState] | None,
) -> _ColumnSnapshot | None:
    """`fqn.column`'s numbers, preferring this run's fresh extraction over the committed print.

    None where `fqn` carries no readable statistics - outside the print, or a column with no
    measured stats (redacted, or older than the field). A catalog-only view column (SPEC
    2.2.15) instead returns `cardinality=None`, which `_compute_observed` filters out.
    """

    if fqn in per_table_meta:
        payload = per_table_meta[fqn].statistics_payload

        if not payload:
            return None

        scope = payload.get("scope")
        scoped = isinstance(scope, dict)
        col = (payload.get("columns") or {}).get(column)

        if not isinstance(col, dict):
            return None

        return _ColumnSnapshot(
            row_count=payload.get("row_count"),
            scoped=scoped,
            sample=scope.get("sample") if scoped else None,
            cardinality=col.get("cardinality"),
            cardinality_method=col.get("cardinality_method"),
            top_count=_top_count(col.get("values")),
            sketch=_decode_column_sketch(col.get("sketch")),
        )

    state = (baseline_states or {}).get(fqn)

    if state is None or state.statistics is None:
        return None

    col = state.statistics.get(column)

    if col is None:
        return None

    return _ColumnSnapshot(
        row_count=state.row_count,
        scoped=state.scoped,
        sample=None,  # unknown from a carried table - see _ColumnSnapshot
        cardinality=col.get("cardinality"),
        cardinality_method=col.get("cardinality_method"),
        top_count=_top_count(col.get("values")),
        sketch=None,  # never hydrated for a carried table - see _ColumnSnapshot
    )


def _decode_column_sketch(sketch: Any) -> tuple[int, ...] | None:
    if not isinstance(sketch, dict) or not isinstance(sketch.get("values"), str):
        return None

    decoded = decode_sketch(sketch["values"])

    return tuple(decoded) if decoded is not None else None


def _top_count(values: Any) -> int | None:
    """`values[0].count` (SPEC 2.2.4 orders by count descending) - the true worst-case group."""

    if not isinstance(values, list) or not values:
        return None

    top = values[0]

    return top.get("count") if isinstance(top, dict) else None


def _scope_compatible(child: _ColumnSnapshot, parent: _ColumnSnapshot) -> bool:
    """SPEC 2.3.10: comparable only when neither side is scoped, or both at a known equal rate."""

    if not child.scoped and not parent.scoped:
        return True

    return (
        child.scoped
        and parent.scoped
        and child.sample is not None
        and child.sample == parent.sample
    )


def _compute_observed(
    child_fqn: str,
    child_column: tuple[str, ...],
    parent_fqn: str,
    parent_column: tuple[str, ...],
    per_table_meta: dict[str, _PerTableContext],
    baseline_states: dict[str, diff_module.TableState] | None,
) -> dict[str, Any] | None:
    """SPEC 2.3.10: what joining across one edge costs, from statistics already on hand.

    None for a composite edge (the format carries no joint cardinality to divide) or
    where either endpoint's column stats are unreachable - SPEC 7.3's absence causes.
    """

    if len(child_column) != 1 or len(parent_column) != 1:
        return None

    child = _column_snapshot(child_fqn, child_column[0], per_table_meta, baseline_states)
    parent = _column_snapshot(parent_fqn, parent_column[0], per_table_meta, baseline_states)

    if child is None or parent is None:
        return None

    if not _scope_compatible(child, parent):
        return {"scope_compatible": False}

    if not child.cardinality or child.row_count is None or not parent.cardinality:
        return None

    observed: dict[str, Any] = {
        "fanout_avg": round(child.row_count / child.cardinality, 6),
        "target_coverage": compute_cardinality_ratio(child.cardinality, parent.cardinality),
        "scope_compatible": True,
    }

    if child.top_count is not None:
        observed["fanout_max"] = child.top_count

    if child.cardinality_method == "exact" and parent.cardinality_method == "exact":
        observed["coherent"] = child.cardinality <= parent.cardinality

    if child.sketch and parent.sketch:
        if len(child.sketch) < SKETCH_K:
            # The child sketch is exhaustive, so containment is measured over the answerable
            # subset, not scaled as for two truncated sketches (SPEC 2.2.14). No ratio, no
            # upgrade: target_coverage keeps the cardinality-derived value set above.
            result = answerable_subset_containment(child.sketch, parent.sketch)

            if result is not None:
                ratio, count = result
                observed["containment"] = min(1.0, round(ratio, 6))
                observed["answerable_count"] = count
                estimated_intersection = round(ratio * child.cardinality)
                observed["target_coverage"] = min(
                    1.0,
                    round(estimated_intersection / parent.cardinality, 6),
                )
        else:
            intersection = estimate_intersection(child.sketch, parent.sketch)
            # target_coverage is upgraded in place, not duplicated (SPEC 2.3.10).
            observed["target_coverage"] = min(1.0, round(intersection / parent.cardinality, 6))
            observed["containment"] = min(1.0, round(intersection / child.cardinality, 6))
            observed["answerable_count"] = answerable_count(child.sketch, parent.sketch)

    return observed


def _build_manifest_entries(
    outcome: _ExtractionOutcome,
    baseline_manifest: dict[str, Any] | None,
    conn: ConnectionConfig,
) -> list[ManifestTableEntry]:
    """Freshly-extracted entries and inherited ones, in the target's listing order.

    Ordering off the matched list rather than the extracted one holds a table in place whether
    it was re-extracted or carried. `outcome.dropped_carried` is excluded from both branches.
    """

    baseline_tables = (baseline_manifest or {}).get("tables") or {}
    dropped = set(outcome.dropped_carried)
    carried = set(outcome.carried_matched) - dropped
    entries: list[ManifestTableEntry] = []

    for fqn in outcome.matched_fqns:
        ctx = outcome.per_table_meta.get(fqn)

        if ctx is not None:
            entries.append(_entry_from_context(fqn, ctx, conn))
        elif fqn in carried:
            entries.append(
                _carried_entry(
                    fqn,
                    baseline_tables[fqn],
                    outcome.resolved_thresholds.get(fqn),
                ),
            )

    for fqn in outcome.carried_out_of_scope:
        if fqn not in dropped:
            entries.append(entry_from_payload(fqn, baseline_tables[fqn]))

    return entries


def _entry_from_context(
    fqn: str,
    ctx: _PerTableContext,
    conn: ConnectionConfig,
) -> ManifestTableEntry:
    return ManifestTableEntry(
        fqn=fqn,
        type=ctx.type,
        path="/".join(ctx.namespace_path),
        has_statistics=ctx.statistics_yaml is not None,
        # `_write_relationships_artifacts` writes for every table: an edgeless view's
        # empty refers_to/referenced_by is still a measurement.
        has_relationships=True,
        has_description=ctx.has_description,
        has_statistics_annotations=ctx.has_statistics_annotations,
        has_relationships_annotations=ctx.has_relationships_annotations,
        row_count=ctx.row_count,
        columns=len(ctx.columns),
        profiled_at=ctx.profiled_at,
        max_age_days=ctx.max_age_days,
        statistics_params=_statistics_override(conn, fqn, ctx.row_count_estimate),
    )


def _statistics_params_dict(cfg: StatisticsConfig) -> dict[str, Any]:
    """A `StatisticsConfig` as a plain dict, with `percentiles` as a list for YAML."""

    values = asdict(cfg)
    values["percentiles"] = list(values["percentiles"])

    return values


def _statistics_override(
    conn: ConnectionConfig,
    fqn: str,
    row_count_estimate: int | None,
) -> dict[str, Any] | None:
    """This table's `StatisticsConfig`, only where it differs from the connection default.

    `row_count_estimate` must be the catalog estimate `settings_for` resolved against (SPEC
    2.5) - the profiled count can sit the other side of a `min_rows` threshold.
    """

    resolved = _statistics_params_dict(conn.settings_for(fqn, row_count_estimate).statistics)
    default = _statistics_params_dict(conn.statistics)
    diff = {key: value for key, value in resolved.items() if value != default[key]}

    return diff or None


def _carried_entry(
    fqn: str,
    payload: dict[str, Any],
    max_age_days: int | None,
) -> ManifestTableEntry:
    """Inherit an entry, restating the threshold when this run resolved one.

    Everything else rides over verbatim, `profiled_at` included - the table was not re-read.
    A run that skipped it as fresh still evaluated it under the value it just resolved.
    """

    entry = entry_from_payload(fqn, payload)

    return entry if max_age_days is None else replace(entry, max_age_days=max_age_days)


def _manifest_unchanged(manifest: dict[str, Any], baseline: dict[str, Any] | None) -> bool:
    """True when the new manifest differs from the committed one only by its timestamp."""

    if not baseline:
        return False

    return {k: v for k, v in manifest.items() if k != "generated_at"} == {
        k: v for k, v in baseline.items() if k != "generated_at"
    }


def _effective_selectors(
    conn: ConnectionConfig,
    cli_include: tuple[str, ...],
    cli_exclude: tuple[str, ...],
) -> tuple[list[str], list[str]]:
    """Config scope merged with the CLI overrides: include narrows, exclude unions."""

    include = list(cli_include) if cli_include else list(conn.include)
    exclude = list(conn.exclude) + [e for e in cli_exclude if e not in conn.exclude]

    return include, exclude


def _carried_matched(
    matched_fqns: tuple[str, ...],
    per_table_meta: dict[str, _PerTableContext],
    baseline_manifest: dict[str, Any] | None,
) -> tuple[str, ...]:
    """Tables the target listed that this run did not re-extract.

    Skipped-as-fresh and failed both land here: the table answered `list_tables`, so it
    exists. Only a table the target no longer lists has been removed.
    """

    entries = (baseline_manifest or {}).get("tables") or {}

    return tuple(f for f in matched_fqns if f not in per_table_meta and f in entries)


def _artifacts_present(prints_root: Path, entry: dict[str, Any]) -> bool:
    """Whether every artifact a manifest entry declares still exists on disk.

    Checked file by file, not by directory: `manifest.missing-artifact` fires on the files
    themselves, and a directory that lost only `statistics.yaml` is the same defect.
    """

    tbl_dir = prints_root / entry.get("path", "")

    return all((tbl_dir / filename).is_file() for filename in _declared_artifacts(entry).values())


def _preserved_incoming(
    prints_root: Path,
    baseline_manifest: dict[str, Any] | None,
    untouched_referencers: tuple[str, ...],
) -> dict[str, list[relationship_graph.IncomingFk]]:
    """Committed incoming edges whose referencer's own file this run left alone.

    Pass 2 resolves the graph from re-extracted tables only, so an untouched referencer's edge
    would vanish from the target print it rewrites in full. `untouched_referencers` is the
    caller's already-filtered set, so a referencer genuinely gone never reaches it.
    """

    if not untouched_referencers:
        return {}

    untouched = set(untouched_referencers)
    out: dict[str, list[relationship_graph.IncomingFk]] = {}

    for fqn, edges in _load_incoming_edges(prints_root, baseline_manifest).items():
        kept = [e for e in edges if e.referencer_table in untouched]

        if kept:
            out[fqn] = kept

    return out


def _merge_incoming(
    resolved: list[relationship_graph.IncomingFk],
    preserved: list[relationship_graph.IncomingFk],
) -> list[relationship_graph.IncomingFk]:
    """Resolved edges plus preserved ones, held in `resolve`'s ordering."""

    if not preserved:
        return resolved

    return sorted([*resolved, *preserved], key=lambda e: (e.referencer_table, e.referencer_column))


def _carried_out_of_scope(
    baseline_manifest: dict[str, Any] | None,
    matched_fqns: tuple[str, ...],
    include: list[str],
    exclude: list[str],
) -> tuple[str, ...]:
    """Committed tables this run's selectors never covered.

    A narrowed run learns nothing about them, so the diff withholds events (SPEC 2.6.8) and
    the manifest must withhold the removal too, or narrowing the scope orphans their prints.
    """

    entries = (baseline_manifest or {}).get("tables") or {}
    matched = set(matched_fqns)

    return tuple(
        f for f in entries if f not in matched and not selectors.match(f, include, exclude)
    )


def _baseline_only_tables(
    baseline_manifest: dict[str, Any] | None,
    matched_fqns: tuple[str, ...],
) -> list[TableMeta]:
    """Baseline-committed tables this run's selectors did not match.

    Stand-ins for the catalog pre-pass alone, which reads only `fqn`/`type`. Inference's
    universe is the committed print's table set, so a narrowed run resolves a stem against the
    same tables a full run would. With no baseline it is this run's matched tables.
    """

    matched = set(matched_fqns)
    entries = (baseline_manifest or {}).get("tables") or {}

    return [
        TableMeta(
            fqn=fqn,
            type=payload.get("type", "table"),
            namespace_path=tuple(p for p in payload.get("path", "").split("/") if p),
        )
        for fqn, payload in entries.items()
        if fqn not in matched
    ]


def _compute_diff_dict(
    project_root: Path,
    *,
    baseline_manifest: dict[str, Any] | None,
    baseline_states: dict[str, diff_module.TableState] | None,
    per_table_meta: dict[str, _PerTableContext],
    carried_matched: tuple[str, ...],
    conn: ConnectionConfig,
    cli_include: tuple[str, ...],
    cli_exclude: tuple[str, ...],
    generated_at: str,
) -> dict[str, Any]:
    """Compare this run's extraction against the states the caller captured.

    `baseline_states` arrive hydrated because the generate path has already overwritten the
    artifacts they come from; the manifest is still read for the provenance fields.
    """

    current_states = {fqn: _table_state_from_context(ctx) for fqn, ctx in per_table_meta.items()}

    # A listed-but-not-re-extracted table stands on its baseline state, so it compares equal
    # to itself: in scope, zero events, not a removal, and still counted by `tables_scanned`,
    # which follows what the selectors matched (SPEC 2.6.3).
    for fqn in carried_matched:
        if baseline_states and fqn in baseline_states:
            current_states[fqn] = baseline_states[fqn]

    # The effective scope (config merged with the CLI overrides), so the diff filters the
    # baseline by what was actually scanned rather than by the config alone (SPEC 2.6.8).
    include, exclude = _effective_selectors(conn, cli_include, cli_exclude)
    diff_selectors = diff_module.DiffSelectors(include=tuple(include), exclude=tuple(exclude))
    baseline_generated_at = baseline_manifest.get("generated_at") if baseline_manifest else None
    baseline_dbprint_version = (
        baseline_manifest.get("dbprint_version") if baseline_manifest else None
    )

    return diff_module.compute(
        baseline_states,
        current_states,
        connection_name=conn.name,
        adapter_kind=conn.adapter,
        baseline_path=_baseline_path(conn, project_root),
        baseline_generated_at=baseline_generated_at,
        baseline_dbprint_version=baseline_dbprint_version,
        scanned_at=generated_at,
        selectors=diff_selectors,
        generated_at=generated_at,
        carried=frozenset(carried_matched),
    )


def _baseline_path(conn: ConnectionConfig, project_root: Path) -> str:
    """Where the baseline prints sit, relative to the project root.

    `conn.output` is absolute after config load, and an absolute path means nothing to a
    reader while leaking the producing machine's layout into a published artifact.
    """

    location = conn.output / conn.name

    try:
        return str(location.relative_to(project_root))
    except ValueError:
        return str(location)


def _empty_diff_dict(
    conn: ConnectionConfig,
    generated_at: str,
    project_root: Path,
) -> dict[str, Any]:
    """Diff payload used when the run aborts before per-table extraction completes."""

    return diff_module.compute(
        baseline=None,
        current={},
        connection_name=conn.name,
        adapter_kind=conn.adapter,
        baseline_path=_baseline_path(conn, project_root),
        baseline_generated_at=None,
        baseline_dbprint_version=None,
        scanned_at=generated_at,
        selectors=diff_module.DiffSelectors(
            include=tuple(conn.include),
            exclude=tuple(conn.exclude),
        ),
        generated_at=generated_at,
    )


def _summarize_diff(diff_dict: dict[str, Any]) -> _DiffResult:
    """Build the typed DiffSummary + has_schema_changes flag from a computed diff dict."""

    s = diff_dict["summary"]
    summary = DiffSummary(
        tables_added=s["tables_added"],
        tables_removed=s["tables_removed"],
        tables_modified=s["tables_modified"],
        columns_added=s["columns_added"],
        columns_removed=s["columns_removed"],
        columns_type_changed=s["columns_type_changed"],
        columns_nullable_changed=s["columns_nullable_changed"],
        columns_default_changed=s["columns_default_changed"],
        statistics_drifted=s["statistics_drifted"],
        relationships_changed=s["relationships_changed"],
        indexes_changed=s["indexes_changed"],
        comments_changed=s["comments_changed"],
        unchanged_tables=s["unchanged_tables"],
        unevaluated_tables=s["unevaluated_tables"],
    )

    return _DiffResult(
        summary=summary,
        has_schema_changes=diff_module.has_schema_changes(diff_dict),
    )


def _statistics_payload_for_assertions(ctx: _PerTableContext) -> dict[str, Any]:
    """The statistics artifact this run would write, as a statistic assertion reads one.

    The assertions.statistic evaluator consumes the committed statistics.yaml shape, so online
    mode hands it this and no predicate re-reads disk.
    """

    payload = ctx.statistics_payload

    return {
        "table": ctx.fqn,
        "type": ctx.type,
        "row_count": payload.get("row_count", 0),
        "columns": payload.get("columns") or {},
    }


def _table_state_from_context(ctx: _PerTableContext) -> diff_module.TableState:
    state = diff_module.TableState(fqn=ctx.fqn, type=ctx.type)
    state.columns = {
        c.name: diff_module.ColumnState(
            name=c.name,
            sql_type=c.sql_type,
            nullable=c.nullable,
            default=c.default,
        )
        for c in ctx.columns
    }
    state.relationships = [
        diff_module.FkState(
            source_columns=fk.column,
            target_table=fk.target_table,
            target_columns=fk.target_column,
            on_delete=fk.on_delete,
            on_update=fk.on_update,
            detection=fk.detection,
        )
        for fk in ctx.relationships
    ]
    state.indexes = {
        idx.name: diff_module.IndexState(
            name=idx.name,
            columns=idx.columns,
            unique=idx.unique,
            type=idx.type,
        )
        for idx in ctx.indexes
    }
    state.table_comment = ctx.comments.table
    state.table_comment_known = True
    state.column_comments = dict(ctx.comments.columns)
    state.statistics = diff_module.comparable_columns(ctx.statistics_payload.get("columns") or {})
    row_count = ctx.statistics_payload.get("row_count")
    state.row_count = row_count if isinstance(row_count, int) else None
    row_count_method = ctx.statistics_payload.get("row_count_method")
    state.row_count_method = row_count_method if isinstance(row_count_method, str) else None
    state.scoped = isinstance(ctx.statistics_payload.get("scope"), dict)
    state.catalog_only = ctx.statistics_payload.get("catalog_only") is True

    # An empty payload leaves grain/physical_layout at the None default - "no data
    # contributed", as for every statistics-derived field. A view carries `grain: {keys: []}`
    # (SPEC 2.2.15) but never `physical_layout`, which stays None either way.
    if ctx.statistics_payload:
        state.grain = diff_module.grain_from_block(ctx.statistics_payload.get("grain"))
        state.physical_layout = diff_module.physical_layout_from_block(
            ctx.statistics_payload.get("physical_layout"),
        )

    return state


def _reread_statistics(statistics_yaml: str) -> dict[str, Any]:
    """Load back the artifact just serialized, as a consumer reads one.

    The dumper renders decimals, timestamps and floats (SPEC 2.2.6) into forms the loader
    hands back differently, so only a round-tripped value compares equal to a committed one.
    """

    loaded = yaml.safe_load(statistics_yaml)

    return loaded if isinstance(loaded, dict) else {}


def _summarize(results: list[TableResult]) -> SummaryCounts:
    counts: dict[TableStatus, int] = {"ok": 0, "skipped": 0, "failed": 0}

    for r in results:
        counts[r.status] += 1

    return SummaryCounts(ok=counts["ok"], skipped=counts["skipped"], failed=counts["failed"])


def _apply_cli_narrowing(
    tables: list[TableMeta],
    cli_include: tuple[str, ...],
    cli_exclude: tuple[str, ...],
) -> list[TableMeta]:
    """Narrow the adapter's listed tables through CLI selectors only.

    Config patterns already filtered the adapter output; a table stays iff it matches some
    cli_include (or there is none) and no cli_exclude, per ARCHITECTURE 6.
    """

    if not cli_include and not cli_exclude:
        return tables

    fqns = [t.fqn for t in tables]
    kept = set(
        selectors.expand(
            fqns,
            config_include=["*"],  # config filtering already happened in list_tables
            config_exclude=[],
            cli_include=list(cli_include) or None,
            cli_exclude=list(cli_exclude) or None,
        ),
    )

    return [t for t in tables if t.fqn in kept]


def _derive_generate_exit_code(
    summary: SummaryCounts,
    schema_drift: bool,
    has_sketch_failures: bool,
) -> int:
    """Worst outcome wins; total failure is distinct from partial.

    Total failure means every table this run touched failed; a skipped table's print is
    already current, so skipped-plus-failed stays partial and all-skipped is EXIT_OK. Only a
    change of shape reaches EXIT_DRIFT, and a sketch-only failure takes EXIT_PARTIAL ahead.
    """

    if summary.failed and not (summary.ok or summary.skipped):
        return EXIT_TOTAL_FAILURE
    elif summary.failed or has_sketch_failures:
        return EXIT_PARTIAL
    elif schema_drift:
        return EXIT_DRIFT
    else:
        return EXIT_OK


def _connection_error_generate(
    connection_name: str,
    generated_at: str,
    started: float,
    exc: Exception,
) -> GenerateResult:
    elapsed_ms = int((time.monotonic() - started) * 1000)

    return GenerateResult(
        connection_name=connection_name,
        tables=(),
        summary=SummaryCounts(ok=0, skipped=0, failed=0),
        diff_summary=DiffSummary(
            tables_added=0,
            tables_removed=0,
            tables_modified=0,
            columns_added=0,
            columns_removed=0,
            columns_type_changed=0,
            columns_nullable_changed=0,
            columns_default_changed=0,
            statistics_drifted=0,
            relationships_changed=0,
            indexes_changed=0,
            comments_changed=0,
            unchanged_tables=0,
            unevaluated_tables=0,
        ),
        elapsed_ms=elapsed_ms,
        exit_code=EXIT_CONNECTION,
        error=str(exc),
    )
