"""`prose` is spelled in three layers and nothing imports it across them.

The engine names it to decide which value enumerations to suppress, the conformance
validator names it to decide what a `text` column owes, and the packaged JSON Schema names
it a third time; the duplication is deliberate, since the conformance suite must not import
the engine. Drift is not: the engine would stop suppressing, or the validator would demand
a list the producer no longer writes, and each layer would still look locally correct - so
a rename takes three coordinated edits, and this test is what fails an uncoordinated one.
"""

from __future__ import annotations

import json
from importlib.resources import files
from typing import Any, get_args

import pytest

from dbprint.conformance import statistics
from dbprint.conformance.schema_validation import check_statistics
from dbprint.engine.orchestrator import _PROSE
from dbprint.spec.looks_like import LooksLike


PATH = "public/t/statistics.yaml"
FQN = "public.t"


def _schema() -> dict[str, Any]:
    return json.loads(
        files("dbprint.spec.v1").joinpath("statistics.schema.json").read_text(encoding="utf-8"),
    )


def test_the_engine_and_the_schema_spell_it_alike() -> None:
    const = _schema()["$defs"]["ValueListUnlessProse"]["if"]["properties"]["inferred"][
        "properties"
    ]["looks_like"]["const"]

    assert const == _PROSE


def test_the_spelling_is_a_pattern_the_detector_can_actually_return() -> None:
    """A literal no detector emits would suppress nothing, silently."""

    assert _PROSE in get_args(LooksLike)


@pytest.mark.parametrize("layer", ["schema", "invariants"])
def test_each_layer_exempts_the_engines_spelling(layer: str) -> None:
    """A text column carrying the engine's literal owes no value list, in both layers."""

    assert _errors(_prose_column(_PROSE), layer) == []


@pytest.mark.parametrize("layer", ["schema", "invariants"])
@pytest.mark.parametrize("spelling", ["Prose", "prosey", "PROSE", "text"])
def test_neither_layer_exempts_a_near_miss(layer: str, spelling: str) -> None:
    """And one that only looks like it still owes the list, so the match is exact."""

    assert _errors(_prose_column(spelling), layer) != []


def _prose_column(spelling: str) -> dict[str, Any]:
    """A `text` column emitted as a producer emits a suppressed one."""

    return {
        "format_version": 1,
        "table": FQN,
        "type": "table",
        "profiled_at": "2026-01-01T00:00:00Z",
        "row_count": 100,
        "row_count_method": "exact",
        "grain": {"keys": []},
        "columns": {
            "field_notes": {
                "sql_type": "TEXT",
                "nullable": False,
                "null_count": 0,
                "null_rate": 0.0,
                "cardinality": 90,
                "cardinality_ratio": 0.9,
                "cardinality_method": "exact",
                "classification": "text",
                "inferred": {"looks_like": spelling},
            },
        },
    }


def _errors(payload: dict[str, Any], layer: str) -> list[Any]:
    issues = (
        check_statistics(payload, PATH)
        if layer == "schema"
        else statistics.check(payload, PATH, FQN)
    )

    return [i for i in issues if i.severity == "error"]
