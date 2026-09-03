"""What the engine writes and renders for a table's grain - what identifies a row (SPEC 2.2.12).

The bounded search belongs to the adapters (`probe_grain`); these cover the half after it,
including the pruning and cap applied before the probe is called and the skips that leave
`search` absent rather than falsely `exhausted`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from dbprint.adapters import (
    ColumnMeta,
    ColumnStats,
    CommentsMeta,
    MockAdapter,
    MockTable,
    UniqueKeyMeta,
)
from dbprint.config import ConnectionConfig, RuleConfig
from dbprint.engine import Engine, GenerateRequest
from dbprint.engine.context_assembler import AssemblyOptions, assemble


class TestDeclaredKeys:
    def test_every_arity_emitted_in_declaration_order(self, tmp_path: Path) -> None:
        payload = _generate(
            tmp_path,
            unique_keys=[
                UniqueKeyMeta(columns=("id",), primary=True),
                UniqueKeyMeta(columns=("a", "b")),
            ],
        )

        assert payload["grain"]["keys"] == [
            {"columns": ["id"], "detection": "declared"},
            {"columns": ["a", "b"], "detection": "declared"},
        ]

    def test_a_single_column_declared_key_still_emits_alongside_candidate_key(
        self,
        tmp_path: Path,
    ) -> None:
        """Restates what `candidate_key` also reports - the two are independent axes."""

        payload = _generate(
            tmp_path,
            unique_keys=[UniqueKeyMeta(columns=("id",), primary=True)],
            candidate_key_column="id",
        )

        assert payload["grain"]["keys"] == [{"columns": ["id"], "detection": "declared"}]
        assert payload["columns"]["id"]["inferred"]["candidate_key"] is True

    def test_no_declared_key_and_no_measured_grain_emits_an_empty_list_not_absence(
        self,
        tmp_path: Path,
    ) -> None:
        payload = _generate(tmp_path, unique_keys=[], candidate_key_column="id")

        assert payload["grain"] == {"keys": []}


class TestMeasuredSearch:
    def test_a_measured_pair_is_emitted_and_marked_measured(self, tmp_path: Path) -> None:
        payload = _generate(tmp_path, unique_keys=[], measured_pairs={("a", "b")})

        assert payload["grain"]["keys"] == [{"columns": ["a", "b"], "detection": "measured"}]
        assert payload["grain"]["search"] == {"exhausted": True}

    def test_arithmetic_pruning_excludes_a_pair_before_any_probe(self, tmp_path: Path) -> None:
        """`lo`'s cardinality makes `a * lo` and `b * lo` fall short of `row_count`; both are
        canned measured-unique, so a skipped prune would echo the canned answer back.
        """

        payload = _generate(
            tmp_path,
            unique_keys=[],
            measured_pairs={("a", "lo"), ("b", "lo")},
            mode="prune",
        )

        assert payload["grain"]["keys"] == []
        assert payload["grain"]["search"] == {"exhausted": True}

    def test_a_null_bearing_column_never_enters_a_candidate_pair(self, tmp_path: Path) -> None:
        payload = _generate(tmp_path, unique_keys=[], measured_pairs={("a", "e")})

        assert payload["grain"]["keys"] == []

    def test_a_declared_pair_is_not_duplicated_by_the_measured_search(
        self,
        tmp_path: Path,
    ) -> None:
        payload = _generate(
            tmp_path,
            unique_keys=[UniqueKeyMeta(columns=("a", "b"))],
            measured_pairs={("a", "b")},
        )

        assert payload["grain"]["keys"] == [{"columns": ["a", "b"], "detection": "declared"}]

    def test_exhausted_true_when_the_pruned_space_fits_the_cap(self, tmp_path: Path) -> None:
        payload = _generate(tmp_path, unique_keys=[])

        assert payload["grain"]["search"] == {"exhausted": True}

    def test_exhausted_false_when_the_cap_cuts_the_search_short(self, tmp_path: Path) -> None:
        """9 null-free columns of equal cardinality make 36 prunable pairs - over the cap of 32."""

        payload = _generate(tmp_path, unique_keys=[], mode="wide")

        assert payload["grain"]["search"] == {"exhausted": False}
        assert len(payload["grain"]["keys"]) <= 32


class TestSkipConditions:
    def test_scope_suppresses_the_measured_search_but_not_declared_keys(
        self,
        tmp_path: Path,
    ) -> None:
        payload = _generate(
            tmp_path,
            unique_keys=[UniqueKeyMeta(columns=("id",), primary=True)],
            measured_pairs={("a", "b")},
            sample=0.5,
        )

        assert payload["grain"]["keys"] == [{"columns": ["id"], "detection": "declared"}]
        assert "search" not in payload["grain"]

    def test_an_empty_table_suppresses_the_measured_search(self, tmp_path: Path) -> None:
        payload = _generate(tmp_path, unique_keys=[], measured_pairs={("a", "b")}, row_count=0)

        assert payload["grain"]["keys"] == []
        assert "search" not in payload["grain"]

    def test_an_existing_candidate_key_suppresses_the_measured_search(
        self,
        tmp_path: Path,
    ) -> None:
        """A single column already answers the question a pair search exists to answer."""

        payload = _generate(
            tmp_path,
            unique_keys=[],
            measured_pairs={("a", "b")},
            candidate_key_column="id",
        )

        assert payload["grain"]["keys"] == []
        assert "search" not in payload["grain"]

    def test_a_near_unique_candidate_key_does_not_suppress_the_measured_search(
        self,
        tmp_path: Path,
    ) -> None:
        """A flag carrying `candidate_key_exception` is a near-uniqueness ratio, not the
        answer a pair search gives (SPEC 4.2) - unlike an exact flag, it suppresses nothing.
        """

        payload = _generate(
            tmp_path,
            unique_keys=[],
            measured_pairs={("a", "b")},
            near_unique_column="id",
            row_count=100_000,
        )

        assert (
            payload["columns"]["id"]["inferred"]["candidate_key_exception"] == "measured_duplicates"
        )
        assert payload["grain"]["keys"] == [{"columns": ["a", "b"], "detection": "measured"}]
        assert payload["grain"]["search"] == {"exhausted": True}


class TestContextRendering:
    def test_a_declared_and_measured_grain_renders_in_the_header(self, tmp_path: Path) -> None:
        text = _context(
            tmp_path,
            unique_keys=[UniqueKeyMeta(columns=("id",), primary=True)],
            measured_pairs=set(),
        )

        assert "Grain: (id) declared" in text

    def test_search_exhausted_with_nothing_found_says_so(self, tmp_path: Path) -> None:
        text = _context(tmp_path, unique_keys=[], measured_pairs=set())

        assert "Grain: searched, none found" in text

    def test_search_bounded_with_nothing_found_says_so(self, tmp_path: Path) -> None:
        text = _context(tmp_path, unique_keys=[], measured_pairs=set(), mode="wide")

        assert "Grain: search bounded, none found within the cap" in text

    def test_a_skipped_search_says_not_determined(self, tmp_path: Path) -> None:
        text = _context(tmp_path, unique_keys=[], measured_pairs=set(), row_count=0)

        assert "Grain: not determined" in text

    def test_the_structured_formats_carry_the_block_whole(self, tmp_path: Path) -> None:
        _generate(tmp_path, unique_keys=[UniqueKeyMeta(columns=("id",), primary=True)])
        root = tmp_path / "w"
        manifest = yaml.safe_load((root / "manifest.yaml").read_text())
        rendered = yaml.safe_load(
            assemble(
                manifest,
                root,
                ["public.wide"],
                AssemblyOptions(format="yaml", include_ddl=False),
            ).text,
        )

        assert rendered["statistics"]["grain"]["keys"] == [
            {"columns": ["id"], "detection": "declared"},
        ]

    def test_a_human_authored_grain_key_rides_beside_the_measured_one(
        self,
        tmp_path: Path,
    ) -> None:
        """SPEC 2.7.1: a human-stated key never replaces the producer's own measurement."""

        unique_keys = [UniqueKeyMeta(columns=("id",), primary=True)]
        _generate(tmp_path, unique_keys=unique_keys)
        root = tmp_path / "w"
        (root / "public" / "wide" / "statistics.annotations.yaml").write_text(
            yaml.safe_dump(
                {
                    "format_version": 1,
                    "columns": {},
                    "grain": {"keys": [{"columns": ["a", "b"], "note": "business key"}]},
                },
            ),
        )
        _regenerate(tmp_path, unique_keys)

        manifest = yaml.safe_load((root / "manifest.yaml").read_text())
        text = assemble(
            manifest,
            root,
            ["public.wide"],
            AssemblyOptions(format="md", include_ddl=False),
        ).text

        assert "Grain: (id) declared; (a, b) annotated" in text

    def test_a_human_authored_grain_key_reaches_the_structured_formats(
        self,
        tmp_path: Path,
    ) -> None:
        unique_keys = [UniqueKeyMeta(columns=("id",), primary=True)]
        _generate(tmp_path, unique_keys=unique_keys)
        root = tmp_path / "w"
        (root / "public" / "wide" / "statistics.annotations.yaml").write_text(
            yaml.safe_dump(
                {
                    "format_version": 1,
                    "columns": {},
                    "grain": {"keys": [{"columns": ["a", "b"], "note": "business key"}]},
                },
            ),
        )
        _regenerate(tmp_path, unique_keys)

        manifest = yaml.safe_load((root / "manifest.yaml").read_text())
        rendered = yaml.safe_load(
            assemble(
                manifest,
                root,
                ["public.wide"],
                AssemblyOptions(format="yaml", include_ddl=False),
            ).text,
        )

        assert rendered["grain_annotations"]["keys"] == [
            {"columns": ["a", "b"], "note": "business key"},
        ]
        # The producer's own measurement is unchanged - still exactly the declared key.
        assert rendered["statistics"]["grain"]["keys"] == [
            {"columns": ["id"], "detection": "declared"},
        ]


def _regenerate(tmp_path: Path, unique_keys: list[UniqueKeyMeta]) -> None:
    """Force a second run: only `generate` probes disk for a new annotation file (SPEC 2.7)."""

    conn = ConnectionConfig(
        name="w",
        adapter="postgres",
        output=tmp_path,
        infer_relationships=False,
    )
    fixture = _fixture(unique_keys, set(), None, 1000, "base")
    Engine(MockAdapter(fixture), conn, tmp_path).generate(GenerateRequest(force=True))


def _generate(
    tmp_path: Path,
    unique_keys: list[UniqueKeyMeta],
    *,
    measured_pairs: set[tuple[str, str]] | None = None,
    candidate_key_column: str | None = None,
    near_unique_column: str | None = None,
    sample: float | None = None,
    row_count: int = 1000,
    mode: str = "base",
) -> dict[str, Any]:
    rules = (RuleConfig(sample=sample),) if sample is not None else ()
    conn = ConnectionConfig(
        name="w",
        adapter="postgres",
        output=tmp_path,
        infer_relationships=False,
        rules=rules,
    )
    fixture = _fixture(
        unique_keys,
        measured_pairs or set(),
        candidate_key_column,
        row_count,
        mode,
        near_unique_column,
    )
    Engine(MockAdapter(fixture), conn, tmp_path).generate()

    return yaml.safe_load((tmp_path / "w" / "public" / "wide" / "statistics.yaml").read_text())


def _context(
    tmp_path: Path,
    unique_keys: list[UniqueKeyMeta],
    measured_pairs: set[tuple[str, str]],
    *,
    row_count: int = 1000,
    mode: str = "base",
) -> str:
    _generate(tmp_path, unique_keys, measured_pairs=measured_pairs, row_count=row_count, mode=mode)
    root = tmp_path / "w"
    manifest = yaml.safe_load((root / "manifest.yaml").read_text())

    return assemble(
        manifest,
        root,
        ["public.wide"],
        AssemblyOptions(format="md", include_ddl=False),
    ).text


# Per `mode`, the null-free columns a candidate pair is drawn from - "base" stays under the
# cap, "prune" adds a pruned column, "wide" exceeds it; `e` carries a null in every mode.
_MODE_COLUMNS: dict[str, tuple[str, ...]] = {
    "base": ("id", "a", "b"),
    "prune": ("a", "b", "lo"),
    "wide": ("w1", "w2", "w3", "w4", "w5", "w6", "w7", "w8", "w9"),
}


def _fixture(
    unique_keys: list[UniqueKeyMeta],
    measured_pairs: set[tuple[str, str]],
    candidate_key_column: str | None,
    row_count: int,
    mode: str,
    near_unique_column: str | None = None,
) -> dict[str, MockTable]:
    # The engine recomputes `candidate_key` from `cardinality`/`rows_scanned` (SPEC 4.2), never
    # from `ColumnStats.inferred`, so `candidate_key_column` works only through cardinality.
    def _column(cardinality: int, *, null_count: int = 0) -> ColumnStats:
        return ColumnStats(
            sql_type="text",
            nullable=null_count > 0,
            null_count=null_count,
            null_rate=null_count / row_count if row_count else 0.0,
            cardinality=cardinality,
            cardinality_ratio=cardinality / row_count if row_count else 0.0,
            cardinality_method="exact",
            values=(),
            values_coverage=1.0,
            distribution="uniform",
        )

    names = (*_MODE_COLUMNS[mode], "e")
    # Half of row_count for an ordinary column, so mutual products clear row_count; 1 for `lo`,
    # whose products fall short. `id` becomes a genuine candidate key when a test asks.
    cardinalities = {name: max(row_count // 2, 1) for name in names}
    cardinalities["lo"] = 1
    cardinalities["e"] = max(row_count // 2, 1)

    if candidate_key_column is not None:
        cardinalities[candidate_key_column] = row_count

    # One short of unique: the ratio still clears SPEC 4.2's threshold, but an exact count short
    # of `row_count` earns `candidate_key_exception: measured_duplicates`, not a clean flag.
    if near_unique_column is not None:
        cardinalities[near_unique_column] = row_count - 1

    columns = [
        ColumnMeta(name=name, sql_type="text", nullable=False, default=None, ordinal=i)
        for i, name in enumerate(names, start=1)
    ]
    stats = {
        name: _column(cardinalities[name], null_count=(1 if name == "e" else 0)) for name in names
    }

    return {
        "public.wide": MockTable(
            type="table",
            namespace_path=("public", "wide"),
            ddl="CREATE TABLE public.wide (placeholder text);\n",
            columns=columns,
            relationships=[],
            indexes=[],
            comments=CommentsMeta(table=None, columns={}),
            stats=stats,
            samples={},
            unique_keys=unique_keys,
            measured_unique_pairs=frozenset(measured_pairs),
            row_count=row_count,
        ),
    }
