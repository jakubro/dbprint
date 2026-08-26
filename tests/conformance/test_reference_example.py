"""The reference example must conform to v1 and match what the producer emits."""

from __future__ import annotations

import importlib.util
import math
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, get_args

import pytest
import yaml

from dbprint import __version__
from dbprint.adapters.base import Distribution
from dbprint.conformance import validate_print
from dbprint.spec.classification import Classification
from dbprint.spec.looks_like import LooksLike
from dbprint.spec.redaction import MASK_PLACEHOLDER, Primitive
from dbprint.spec.sensitivity import Sensitivity
from tests.conftest import PostgresCluster, normalize_print_tree
from tests.spec import _spec_markdown


REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = REPO_ROOT / "docs/format/v1/examples/production/prints/production"
EXAMPLE_VOCABULARY = REPO_ROOT / "docs/format/v1/examples/vocabulary/prints/vocabulary"
README = REPO_ROOT / "docs/format/v1/examples/README.md"

_BACKTICKED = re.compile(r"`([^`]+)`")
_BACKTICKED_NAME = re.compile(r"^`([^`]+)`$")
_OBJECT_HEADING = re.compile(r"\*\*(\w+) objects:\*\*")
_NUMBER_WORDS = {
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
}


def test_reference_example_has_zero_errors() -> None:
    issues = validate_print(EXAMPLE)
    errors = [i for i in issues if i.severity == "error"]
    assert errors == [], "Reference example must conform with zero errors. Got:\n" + "\n".join(
        f"  {e.code} at {e.path}: {e.detail}" for e in errors
    )


def test_vocabulary_example_has_zero_errors() -> None:
    issues = validate_print(EXAMPLE_VOCABULARY)
    errors = [i for i in issues if i.severity == "error"]
    assert errors == [], "Vocabulary example must conform with zero errors. Got:\n" + "\n".join(
        f"  {e.code} at {e.path}: {e.detail}" for e in errors
    )


def test_committed_examples_record_the_current_producer_version() -> None:
    """A version bump restages both examples; fail here, not in the Postgres-backed agreement."""

    recorded: list[tuple[str, str]] = []

    for root in (EXAMPLE, EXAMPLE_VOCABULARY):
        for name in ("manifest.yaml", "diff.yaml"):
            document = yaml.safe_load((root / name).read_text())
            nested = (value for value in document.values() if isinstance(value, dict))

            recorded.extend(
                (f"{root.name}/{name}", block["dbprint_version"])
                for block in (document, *nested)
                if block.get("dbprint_version") is not None
            )

    assert recorded, "no committed example records a producer version"

    stale = [(where, value) for where, value in recorded if value != __version__]

    assert not stale, (
        f"the committed examples record a producer other than {__version__}; "
        "run `just example` and `just example-vocabulary`.\n"
        + "\n".join(f"  {where}: {value}" for where, value in stale)
    )


class TestTheExampleTeachesTheFieldsItIsTheDemonstrationOf:
    """An independent producer learns v1 from this directory and from nothing else.

    A normative field no committed artifact carries cannot be learned here.
    """

    def test_a_committed_column_declares_a_redaction(self) -> None:
        assert _marked_columns(), "no committed column carries a `redacted` marker"

    def test_a_redacted_column_names_the_category_it_was_matched_on(self) -> None:
        """A sensitivity-keyed rule redacts a column whose artifact shows what was detected."""

        sensitivity_matched = [
            name
            for name, column in _marked_columns()
            if column.get("inferred", {}).get("sensitivity")
        ]

        assert sensitivity_matched, "no redacted column was matched via a detected sensitivity"

    def test_a_redacted_column_can_be_matched_by_glob_alone(self) -> None:
        """A column-glob rule redacts a column with no sensitivity category - the escape hatch."""

        glob_matched = [
            name
            for name, column in _marked_columns()
            if not column.get("inferred", {}).get("sensitivity")
        ]

        assert glob_matched, "no redacted column demonstrates the column-glob escape hatch"

    def test_a_redaction_withholds_the_literals_and_keeps_the_measurements(self) -> None:
        """SPEC 2.2.9: a primitive withholds `value`, never the counts or cardinality."""

        checked = set()

        for payload in _statistics().values():
            for name, column in payload["columns"].items():
                values = column.get("values")
                primitive = column.get("redacted")

                if primitive is None or values is None:
                    continue

                non_null = payload["row_count"] - column["null_count"]

                if primitive == "mask":
                    assert {e["value"] for e in values} == {MASK_PLACEHOLDER}, name
                elif primitive == "drop":
                    assert all("value" not in e for e in values), name
                elif primitive == "hash":
                    literals = [e["value"] for e in values]
                    assert MASK_PLACEHOLDER not in literals, name
                    assert len(set(literals)) == len(literals), name

                assert column["cardinality"] >= len(values), name
                assert (
                    abs(column["values_coverage"] - sum(e["count"] for e in values) / non_null)
                    < 1e-6
                ), name
                checked.add(primitive)

        assert checked == {"mask", "drop", "hash"}, f"not all primitives exercised: {checked}"

    def test_a_marker_sits_only_where_a_literal_was_published(self) -> None:
        """A marker on a column that emitted no cell value announces a redaction of nothing."""

        for name, column in _marked_columns():
            assert any(key in column for key in ("values", "range", "percentiles")), name

    def test_a_fully_enumerated_column_carries_a_shape(self) -> None:
        """The small-column case: every literal published, and a claim about their shape."""

        enumerated = [
            name
            for payload in _statistics().values()
            for name, column in payload["columns"].items()
            if column.get("values_coverage") == 1.0 and column.get("inferred", {}).get("looks_like")
        ]

        assert enumerated, "no column pairs an exhaustive value list with a `looks_like`"

    def test_every_classification_the_spec_defines_appears(self) -> None:
        """examples/README.md claims all nine; assert against the spec's own set, not a count."""

        present = {
            column["classification"]
            for payload in _statistics().values()
            for column in payload["columns"].values()
        }

        assert present == set(get_args(Classification))

    def test_every_looks_like_pattern_the_spec_defines_appears(self) -> None:
        """No single example directory need show every pattern; the union of the two must."""

        present = {
            column["inferred"]["looks_like"]
            for example in (EXAMPLE, EXAMPLE_VOCABULARY)
            for payload in _statistics(example).values()
            for column in payload["columns"].values()
            if column.get("inferred", {}).get("looks_like")
        }

        assert present == set(get_args(LooksLike))

    def test_every_sensitivity_category_the_spec_defines_appears(self) -> None:
        """No single example directory need show every category; the union of the two must."""

        present = {
            column["inferred"]["sensitivity"]
            for example in (EXAMPLE, EXAMPLE_VOCABULARY)
            for payload in _statistics(example).values()
            for column in payload["columns"].values()
            if column.get("inferred", {}).get("sensitivity")
        }

        assert present == set(get_args(Sensitivity))

    def test_every_redaction_primitive_the_spec_defines_appears(self) -> None:
        present = {
            column["redacted"]
            for payload in _statistics().values()
            for column in payload["columns"].values()
            if "redacted" in column
        }

        assert present == set(get_args(Primitive))

    def test_every_distribution_value_the_spec_defines_appears(self) -> None:
        present = {
            column["distribution"]
            for payload in _statistics().values()
            for column in payload["columns"].values()
            if "distribution" in column
        }

        assert present == set(get_args(Distribution))

    def test_the_manifest_declares_every_file_the_print_holds(self) -> None:
        """SPEC 2.5: a manifest disagreeing with the directory is an inconsistent print."""

        manifest = yaml.safe_load((EXAMPLE / "manifest.yaml").read_text())

        for fqn, entry in manifest["tables"].items():
            on_disk = {p.name for p in (EXAMPLE / entry["path"]).iterdir() if p.is_file()}

            assert on_disk == set(entry.get("artifacts", {}).values()), fqn


class TestFreshnessAgreesWithItsOwnFields:
    """SPEC 2.2.4: `max_age_days` is the whole elapsed days from `range.max` to `profiled_at`,
    recomputed without `dbprint.spec.temporal_age` so guard and producer cannot agree wrongly.
    """

    def test_every_temporal_columns_max_age_matches_its_own_range(self) -> None:
        checked = 0

        for payload in _statistics().values():
            profiled_at = _parse_utc(payload["profiled_at"])
            assert profiled_at is not None, "profiled_at is ALWAYS a parseable ISO instant"

            for name, column in payload["columns"].items():
                freshness = column.get("freshness")
                range_block = column.get("range")

                if freshness is None or range_block is None or "redacted" in column:
                    continue

                range_max = _parse_utc(range_block.get("max"))

                if range_max is None:
                    continue

                elapsed = math.floor((profiled_at - range_max).total_seconds() / 86400)
                assert freshness["max_age_days"] == max(0, elapsed), name
                checked += 1

        assert checked >= 2, "expected at least the two example temporal columns to be checked"


class TestProducerAgreement:
    """The committed example must be what a real run emits, byte for byte.

    DDL is compared too, so a failure can mean `pg_dump`'s output moved: re-run `just example`.
    """

    def test_regenerating_reproduces_the_committed_tree(
        self,
        postgres_cluster: PostgresCluster,
        tmp_path: Path,
    ) -> None:
        generator = _load_generator()
        credentials = _fresh_database(postgres_cluster, generator)

        generator.build_example(credentials, tmp_path / "example")
        regenerated = normalize_print_tree(
            tmp_path / "example" / "prints" / generator.CONNECTION,
        )
        committed = normalize_print_tree(EXAMPLE)

        assert sorted(regenerated) == sorted(committed), "the set of emitted files moved"

        differing = [path for path in committed if committed[path] != regenerated[path]]
        assert not differing, (
            "the committed example is not what the producer emits; run `just example`.\n"
            + "\n".join(f"  {path}" for path in differing)
        )


class TestVocabularyProducerAgreement:
    """The committed vocabulary example must be what a real run emits, byte for byte."""

    def test_regenerating_reproduces_the_committed_tree(
        self,
        postgres_cluster: PostgresCluster,
        tmp_path: Path,
    ) -> None:
        generator = _load_generator("gen_vocabulary_example")
        credentials = _fresh_database(postgres_cluster, generator)

        generator.build_example(credentials, tmp_path / "example")
        regenerated = normalize_print_tree(
            tmp_path / "example" / "prints" / generator.CONNECTION,
        )
        committed = normalize_print_tree(EXAMPLE_VOCABULARY)

        assert sorted(regenerated) == sorted(committed), "the set of emitted files moved"

        differing = [path for path in committed if committed[path] != regenerated[path]]
        assert not differing, (
            "the committed vocabulary example is not what the producer emits; "
            "run `just example-vocabulary`.\n" + "\n".join(f"  {path}" for path in differing)
        )


class TestComparisonIsClockIndependent:
    """The golden compares producer decisions, so nothing it reads may move with the calendar.

    `freshness` is derived from `range.max` and the run's own stamped instant (SPEC 2.2.4),
    never a live clock, so two runs over the same data agree on it without normalization.
    """

    @staticmethod
    def _write(root: Path, text: str) -> Path:
        path = root / "statistics.yaml"
        path.write_text(text)

        return path

    def _normalized(self, tmp_path: Path, name: str, text: str) -> dict[str, str]:
        root = tmp_path / name
        root.mkdir()
        self._write(root, text)

        return normalize_print_tree(root)

    def test_a_real_difference_still_shows(self, tmp_path: Path) -> None:
        one = self._normalized(tmp_path, "one", _freshness_yaml(47, "stale", cardinality=10))
        other = self._normalized(tmp_path, "other", _freshness_yaml(47, "stale", cardinality=11))

        assert one != other

    def test_run_stamps_still_collapse(self, tmp_path: Path) -> None:
        one = self._normalized(tmp_path, "one", _temporal_yaml(profiled_at="2026-01-01T00:00:00Z"))
        other = self._normalized(
            tmp_path,
            "other",
            _temporal_yaml(profiled_at="2026-01-02T00:00:00Z"),
        )

        assert one == other

    def test_a_mutated_percentile_is_not_absorbed(self, tmp_path: Path) -> None:
        one = self._normalized(tmp_path, "one", _temporal_yaml(p50="2021-01-01T00:00:00Z"))
        other = self._normalized(tmp_path, "other", _temporal_yaml(p50="2021-01-02T00:00:00Z"))

        assert one != other

    def test_an_equally_shifted_range_pair_is_not_absorbed(self, tmp_path: Path) -> None:
        """A blanket instant match would collapse both bounds and hide the shift entirely."""

        one = self._normalized(
            tmp_path,
            "one",
            _temporal_yaml(range_min="2020-01-01T00:00:00Z", range_max="2022-01-01T00:00:00Z"),
        )
        other = self._normalized(
            tmp_path,
            "other",
            _temporal_yaml(range_min="2020-01-02T00:00:00Z", range_max="2022-01-02T00:00:00Z"),
        )

        assert one != other


class TestAnnotationSeedingIsSearchBased:
    """`_seed_statistics_annotations` finds its sources by filename, not a maintained path list."""

    def test_every_committed_annotations_file_is_copied(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        generator = _load_generator()
        source_root = tmp_path / "committed"
        (source_root / "public" / "curator").mkdir(parents=True)
        (source_root / "public" / "cultivar").mkdir(parents=True)
        (source_root / "public" / "curator" / "statistics.annotations.yaml").write_text(
            "format_version: 1\ncolumns: {}\n",
        )
        (source_root / "public" / "cultivar" / "statistics.annotations.yaml").write_text(
            "format_version: 1\ncolumns: {}\n",
        )
        monkeypatch.setattr(generator, "PRINT_ROOT", source_root)

        destination_root = tmp_path / "fresh"
        destination_root.mkdir()
        generator._seed_statistics_annotations(destination_root)

        assert (destination_root / "public" / "curator" / "statistics.annotations.yaml").is_file()
        assert (destination_root / "public" / "cultivar" / "statistics.annotations.yaml").is_file()

    def test_an_existing_destination_file_is_left_alone(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        generator = _load_generator()
        source_root = tmp_path / "committed"
        (source_root / "public" / "curator").mkdir(parents=True)
        (source_root / "public" / "curator" / "statistics.annotations.yaml").write_text(
            "committed",
        )
        monkeypatch.setattr(generator, "PRINT_ROOT", source_root)

        destination_root = tmp_path / "fresh" / "public" / "curator"
        destination_root.mkdir(parents=True)
        (destination_root / "statistics.annotations.yaml").write_text("already there")
        generator._seed_statistics_annotations(tmp_path / "fresh")

        assert (destination_root / "statistics.annotations.yaml").read_text() == "already there"


def _freshness_yaml(max_age_days: int, classification: str, cardinality: int = 10) -> str:
    return (
        "columns:\n"
        "  created_at:\n"
        f"    cardinality: {cardinality}\n"
        "    freshness:\n"
        f"      max_age_days: {max_age_days}\n"
        f"      classification: {classification}\n"
    )


def _temporal_yaml(
    profiled_at: str = "2026-01-01T00:00:00Z",
    range_min: str = "2020-01-01T00:00:00Z",
    range_max: str = "2022-01-01T00:00:00Z",
    p50: str = "2021-01-01T00:00:00Z",
) -> str:
    """A minimal temporal column, for asserting what the normalizer does and does not collapse."""

    return (
        f"profiled_at: '{profiled_at}'\n"
        "columns:\n"
        "  created_at:\n"
        "    range:\n"
        f"      min: '{range_min}'\n"
        f"      max: '{range_max}'\n"
        "    percentiles:\n"
        f"      p50: '{p50}'\n"
    )


def _statistics(example: Path = EXAMPLE) -> dict[str, dict[str, Any]]:
    """Every committed statistics.yaml the manifest declares, keyed by table."""

    manifest = yaml.safe_load((example / "manifest.yaml").read_text())
    out: dict[str, dict[str, Any]] = {}

    for fqn, entry in manifest["tables"].items():
        artifacts = entry.get("artifacts", {})

        if "statistics" in artifacts:
            out[fqn] = yaml.safe_load(
                (example / entry["path"] / artifacts["statistics"]).read_text(),
            )

    return out


def _parse_utc(value: str | None) -> datetime | None:
    """A published ISO instant as a UTC-aware `datetime`, or None when unparseable.

    A bare date or zone-less timestamp parses naive; SPEC 2.2.4 requires reading it as UTC.
    """

    if not isinstance(value, str):
        return None

    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None

    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _marked_columns() -> list[tuple[str, dict[str, Any]]]:
    """Every committed column carrying a `redacted` marker, as (name, payload)."""

    return [
        (name, column)
        for payload in _statistics().values()
        for name, column in payload["columns"].items()
        if "redacted" in column
    ]


def _load_generator(module_name: str = "gen_reference_example") -> Any:
    path = REPO_ROOT / "scripts" / f"{module_name}.py"
    spec = importlib.util.spec_from_file_location(module_name, path)

    if spec is None or spec.loader is None:
        pytest.fail(f"could not load {path}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    return module


def _fresh_database(cluster: PostgresCluster, generator: Any) -> dict[str, str]:
    credentials = {
        "host": "127.0.0.1",
        "port": str(cluster.port),
        "database": generator.DATABASE,
        "user": cluster.superuser,
        "password": "postgres",
    }
    generator._create_database(credentials)

    return credentials


class TestReadmeAgreement:
    """The example's README against the tree it describes.

    Every other file there is producer-written and byte-compared; the README is hand-written,
    so only what it enumerates can be checked - prose is deliberately out of scope. Covers the
    production example only; the vocabulary example's own `sensitivity` list is a different
    shape and would need its own parser.
    """

    def test_the_object_table_names_exactly_what_the_manifest_declares(self) -> None:
        assert _readme_objects() == set(_manifest()["tables"])

    def test_the_heading_count_matches_the_object_set(self) -> None:
        stated = _readme_object_count()

        assert stated == len(_manifest()["tables"])

    def test_every_coverage_cell_holds_the_columns_that_classify_that_way(self) -> None:
        assert _readme_coverage() == _committed_classifications()


def _manifest() -> dict[str, Any]:
    return yaml.safe_load((EXAMPLE / "manifest.yaml").read_text())


def _readme_block(start: str, end: str) -> str:
    return _spec_markdown.section_of(README, start, end)


def _readme_objects() -> set[str]:
    """The fully-qualified names the object table's first column carries.

    Found by the heading's shape rather than its wording, so changing the count does not
    have to be done here too.
    """

    text = README.read_text()
    heading = _OBJECT_HEADING.search(text)

    assert heading is not None, "the object table has no '**<count> objects:**' heading"

    rows = _spec_markdown.table_rows(text[heading.end() : text.index("## Coverage matrix")])

    return {match.group(1) for cells in rows if (match := _BACKTICKED_NAME.match(cells[0].strip()))}


def _readme_object_count() -> int:
    """The count the object table's heading states, read as a word."""

    match = _OBJECT_HEADING.search(README.read_text())

    assert match is not None, "the object table has no '**<count> objects:**' heading"

    return _NUMBER_WORDS[match.group(1).lower()]


def _readme_coverage() -> dict[str, set[str]]:
    """The Coverage matrix as {classification: {table.column}}.

    A cell continues the previous table with a leading dot, and names a bare type in
    parentheses where a column needs one; only dotted entries are column references.
    """

    out: dict[str, set[str]] = {}

    for cells in _spec_markdown.table_rows(_readme_block("## Coverage matrix", "## Inferred")):
        name = cells[0].strip("`")

        if name == "Classification" or len(cells) < 2:
            continue

        entries: set[str] = set()
        previous = ""

        for item in _BACKTICKED.findall(cells[1]):
            resolved = f"{previous}{item}" if item.startswith(".") else item

            if "." not in resolved:
                continue

            parts = resolved.split(".")
            previous = ".".join(parts[:-1])
            entries.add(".".join(parts[-2:]))

        out[name] = entries

    return out


def _committed_classifications() -> dict[str, set[str]]:
    """{classification: {table.column}} over every statistics file the manifest declares."""

    out: dict[str, set[str]] = {}

    for fqn, entry in _manifest()["tables"].items():
        name = entry.get("artifacts", {}).get("statistics")

        if not name:
            continue

        stats = yaml.safe_load((EXAMPLE / entry["path"] / name).read_text())
        short = fqn.split(".")[-1]

        for column, body in (stats.get("columns") or {}).items():
            if classification := body.get("classification"):
                out.setdefault(classification, set()).add(f"{short}.{column}")

    return out
