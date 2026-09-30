"""The validator's YAML reader: instants as ISO strings, numbers keeping their written text."""

from __future__ import annotations

from pathlib import Path

from dbprint.conformance.yaml_utils import SourcedFloat, SourcedInt, load_yaml


def _load(tmp_path: Path, text: str) -> object:
    path = tmp_path / "a.yaml"
    path.write_text(text, encoding="utf-8")

    return load_yaml(path)


def test_a_utc_instant_reads_as_an_iso_string_ending_in_z(tmp_path: Path) -> None:
    assert _load(tmp_path, "at: 2026-05-17T22:48:01Z\n") == {"at": "2026-05-17T22:48:01Z"}


def test_an_offset_instant_keeps_its_offset(tmp_path: Path) -> None:
    assert _load(tmp_path, "at: 2026-05-17T22:48:01+02:00\n") == {"at": "2026-05-17T22:48:01+02:00"}


def test_a_date_reads_as_an_iso_string(tmp_path: Path) -> None:
    assert _load(tmp_path, "day: 2026-05-17\n") == {"day": "2026-05-17"}


def test_instants_nested_in_lists_and_maps_are_read_the_same_way(tmp_path: Path) -> None:
    text = "rows:\n- day: 2026-05-17\n  at: [2026-05-17T00:00:00Z]\n"

    assert _load(tmp_path, text) == {
        "rows": [{"day": "2026-05-17", "at": ["2026-05-17T00:00:00Z"]}],
    }


def test_a_number_keeps_the_text_it_was_written_as(tmp_path: Path) -> None:
    loaded = _load(tmp_path, "whole: +12\nwide: 1.12345678901234567890\n")

    assert isinstance(loaded, dict)
    assert type(loaded["whole"]) is SourcedInt
    assert loaded["whole"] == 12
    assert loaded["whole"].source == "+12"
    assert type(loaded["wide"]) is SourcedFloat
    assert loaded["wide"].source == "1.12345678901234567890"


def test_text_and_other_scalars_pass_through(tmp_path: Path) -> None:
    assert _load(tmp_path, "name: seed\nflag: true\nnothing: null\n") == {
        "name": "seed",
        "flag": True,
        "nothing": None,
    }
