"""Manifest assembly and the round-trip that carries entries between runs (SPEC 2.5).

An optional key survives `build` -> `carried_entry` both ways, or a skipped table rewrites its entry.
"""

from __future__ import annotations

import json
from importlib import resources
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from dbprint.config import LooksLikeProfile, ProfileConfig, ReadConfig, ReadSettings
from dbprint.engine.carried import CarriedTable, CommittedTable, carried_entry
from dbprint.engine.diff import DiffSelectors
from dbprint.engine.manifest_builder import (
    ManifestTableEntry,
    build,
    profile_delta,
    profile_dict,
)
from dbprint.spec.profile import effective_profile


DEFAULT_PROFILE: dict[str, Any] = {
    "read": {"tables": {"mode": "full"}, "views": {"mode": "catalog"}},
    "statistics": {
        "enumeration_threshold": 50,
        "top_n_values": 20,
        "percentiles": [1, 25, 50, 75, 99],
    },
    "looks_like": {"enabled": True, "sample_size": 1000},
    "null_patterns": {"enabled": True, "top_n": 20},
    "parts": {"enabled": True, "max": 20, "max_depth": 3},
    "timeline": {"enabled": True},
    "sketches": {"enabled": True, "columns": "required"},
    "relationships": {"infer": True},
}
NO_SELECTORS = DiffSelectors(include=("*",), exclude=())
_MANIFEST_DEFS = json.loads(
    resources.files("dbprint.spec.v1").joinpath("manifest.schema.json").read_text("utf-8"),
)["$defs"]


def _carried(fqn: str, payload: dict[str, Any]) -> ManifestTableEntry:
    carried = CarriedTable(table=CommittedTable(fqn=fqn, entry=payload), reason="out_of_scope")

    return carried_entry(carried, resolved_max_age_days=None)


def _entry(**overrides: Any) -> ManifestTableEntry:
    """Defaults to seedbank.accession's identity; both annotation flags start off."""

    fields: dict[str, Any] = {
        "fqn": "seedbank.accession",
        "type": "table",
        "path": "seedbank/accession",
        "has_statistics": True,
        "has_relationships": True,
        "has_description": True,
        "has_statistics_annotations": False,
        "has_relationships_annotations": False,
        "row_count": 2500,
        "columns": 16,
        "profiled_at": "2026-03-09T14:27:36Z",
    }
    fields.update(overrides)

    return ManifestTableEntry(**fields)


def _matview_entry(**overrides: Any) -> ManifestTableEntry:
    """germination_by_taxon_mv's identity: refreshed every 30 days, not daily (SPEC 2.5)."""

    fields: dict[str, Any] = {
        "fqn": "seedbank.germination_by_taxon_mv",
        "type": "matview",
        "path": "seedbank/germination_by_taxon_mv",
        "row_count": 770,
        "columns": 4,
    }
    fields.update(overrides)

    return _entry(**fields)


def _manifest(entries: list[ManifestTableEntry], **overrides: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "connection_name": "primary",
        "adapter_kind": "postgres",
        "entries": entries,
        "generated_at": "2026-08-01T00:00:00Z",
        "profile": DEFAULT_PROFILE,
        "selectors": NO_SELECTORS,
        "redaction_rules_configured": 0,
        "default_collation": "en_US.UTF-8",
    }
    fields.update(overrides)

    return build(**fields)


def _payload(entry: ManifestTableEntry, **overrides: Any) -> dict[str, Any]:
    return _manifest([entry], **overrides)["tables"][entry.fqn]


class TestFreshnessThreshold:
    def test_a_resolved_threshold_is_recorded(self) -> None:
        assert _payload(_matview_entry(max_age_days=30))["max_age_days"] == 30

    def test_an_unrecorded_threshold_emits_no_key(self) -> None:
        """A null would claim the producer resolved nothing, which is a different thing."""

        assert "max_age_days" not in _payload(_matview_entry())

    def test_the_threshold_survives_a_carry_forward(self) -> None:
        carried = _carried(
            "seedbank.germination_by_taxon_mv",
            _payload(_matview_entry(max_age_days=30)),
        )

        assert carried.max_age_days == 30
        assert _payload(carried)["max_age_days"] == 30

    def test_an_entry_from_a_producer_that_recorded_none_stays_that_way(self) -> None:
        """The older-manifest path: absent must not become a key on the next run."""

        payload = _payload(_matview_entry())
        carried = _carried("seedbank.germination_by_taxon_mv", payload)

        assert carried.max_age_days is None
        assert "max_age_days" not in _payload(carried)


class TestAnnotationsPresence:
    def test_present_when_authored(self) -> None:
        assert _payload(_entry(has_statistics_annotations=True))["artifacts"][
            "statistics_annotations"
        ] == ("statistics.annotations.yaml")

    def test_absent_when_not_authored(self) -> None:
        assert "statistics_annotations" not in _payload(_entry())["artifacts"]

    def test_presence_survives_a_carry_forward(self) -> None:
        carried = _carried(
            "seedbank.accession",
            _payload(_entry(has_statistics_annotations=True)),
        )

        assert carried.has_statistics_annotations is True
        assert "statistics_annotations" in _payload(carried)["artifacts"]


class TestRelationshipAnnotationsPresence:
    def test_present_when_authored(self) -> None:
        assert _payload(_entry(has_relationships_annotations=True))["artifacts"][
            "relationships_annotations"
        ] == ("relationships.annotations.yaml")

    def test_absent_when_not_authored(self) -> None:
        assert "relationships_annotations" not in _payload(_entry())["artifacts"]

    def test_presence_survives_a_carry_forward(self) -> None:
        carried = _carried(
            "seedbank.accession",
            _payload(_entry(has_relationships_annotations=True)),
        )

        assert carried.has_relationships_annotations is True
        assert "relationships_annotations" in _payload(carried)["artifacts"]


class TestManifestAnnotationsPresence:
    """SPEC 2.7.3: `manifest_annotations` is a top-level flag, not a per-table one."""

    def test_present_when_authored(self) -> None:
        manifest = _manifest([_entry()], has_manifest_annotations=True)

        assert manifest["manifest_annotations"] == "manifest.annotations.yaml"

    def test_absent_when_not_authored(self) -> None:
        assert "manifest_annotations" not in _manifest([_entry()])


class TestProvenance:
    """SPEC 2.5: the manifest records what produced the print."""

    def test_the_default_profile_records_every_block(self) -> None:
        assert profile_dict(ProfileConfig()) == DEFAULT_PROFILE
        assert _manifest([_entry()])["profile"] == DEFAULT_PROFILE

    def test_a_switched_off_feature_records_no_parameters(self) -> None:
        profile = ProfileConfig(looks_like=LooksLikeProfile(enabled=False, sample_size=7))

        assert profile_dict(profile)["looks_like"] == {"enabled": False}

    def test_a_read_block_records_only_its_modes_parameters(self) -> None:
        read = ReadSettings(tables=ReadConfig(mode="filter", filter="id > 0", max_rows=9))

        assert profile_dict(ProfileConfig(read=read))["read"]["tables"] == {
            "mode": "filter",
            "filter": "id > 0",
        }

    @pytest.mark.parametrize(
        "read",
        [
            ReadSettings(),
            ReadSettings(tables=ReadConfig(mode="sample", fraction=0.25)),
            ReadSettings(tables=ReadConfig(mode="sample", max_rows=500, materialize=False)),
            ReadSettings(views=ReadConfig(mode="filter", filter="id > 0")),
        ],
    )
    def test_the_schema_requires_every_leaf_the_producer_records(self, read: ReadSettings) -> None:
        recorded = profile_dict(ProfileConfig(read=read))
        validator = Draft202012Validator({"$ref": "#/$defs/Profile", "$defs": _MANIFEST_DEFS})

        assert validator.is_valid(recorded)

        for path in _leaves(recorded):
            assert not validator.is_valid(_without(recorded, path)), path

    def test_a_delta_names_each_differing_block_whole(self) -> None:
        sampled = {**DEFAULT_PROFILE, "read": {**DEFAULT_PROFILE["read"]}}
        sampled["read"]["tables"] = {"mode": "sample", "max_rows": 9, "materialize": True}
        ceiling = {**sampled, "read": {**sampled["read"]}}
        ceiling["read"]["tables"] = {"mode": "sample", "fraction": 0.5, "materialize": True}
        delta = profile_delta(ceiling, sampled)

        assert delta == {"read": {"tables": ceiling["read"]["tables"]}}
        assert effective_profile(sampled, delta) == ceiling
        assert profile_delta(DEFAULT_PROFILE, DEFAULT_PROFILE) is None

    def test_selectors_recorded_as_applied(self) -> None:
        manifest = _manifest(
            [_entry()],
            selectors=DiffSelectors(include=("public.*",), exclude=("public.curation_event",)),
        )

        assert manifest["selectors"] == {
            "include": ["public.*"],
            "exclude": ["public.curation_event"],
        }

    def test_redaction_rules_configured_recorded(self) -> None:
        assert (
            _manifest([_entry()], redaction_rules_configured=3)["redaction_rules_configured"] == 3
        )

    def test_default_collation_recorded(self) -> None:
        assert _manifest([_entry()], default_collation="C")["default_collation"] == "C"

    def test_a_table_with_no_override_carries_no_profile(self) -> None:
        assert "profile" not in _payload(_entry())

    def test_a_tables_own_override_is_recorded(self) -> None:
        override = {"null_patterns": {"enabled": False}}
        assert _payload(_entry(profile=override))["profile"] == override

    def test_a_tables_override_survives_a_carry_forward(self) -> None:
        override = {"null_patterns": {"enabled": False}}
        carried = _carried("seedbank.accession", _payload(_entry(profile=override)))

        assert carried.profile == override
        assert _payload(carried)["profile"] == override

    def test_an_entry_from_a_producer_that_recorded_none_stays_that_way(self) -> None:
        carried = _carried("seedbank.accession", _payload(_entry()))

        assert carried.profile is None
        assert "profile" not in _payload(carried)


def _leaves(tree: dict[str, Any], prefix: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
    return [
        leaf
        for key, value in tree.items()
        for leaf in (
            _leaves(value, (*prefix, key)) if isinstance(value, dict) else [(*prefix, key)]
        )
    ]


def _without(tree: dict[str, Any], path: tuple[str, ...]) -> dict[str, Any]:
    head, *rest = path

    return {
        key: (_without(value, tuple(rest)) if key == head and rest else value)
        for key, value in tree.items()
        if key != head or rest
    }
