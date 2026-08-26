"""Manifest assembly and the round-trip that carries entries between runs (SPEC 2.5).

An optional key has to survive `build` -> `entry_from_payload` in both directions - present
stays present, absent stays absent - or a run that skips a table rewrites its entry.
"""

from __future__ import annotations

from typing import Any

from dbprint.engine.diff import DiffSelectors
from dbprint.engine.manifest_builder import ManifestTableEntry, build, entry_from_payload


DEFAULT_STATISTICS_PARAMS: dict[str, Any] = {
    "enumeration_threshold": 50,
    "top_n_values": 20,
    "top_n_null_patterns": 20,
    "looks_like_sample_size": 1000,
    "percentiles": [1, 25, 50, 75, 99],
}
NO_SELECTORS = DiffSelectors(include=("*",), exclude=())


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
        "profiled_at": "2026-05-17T22:48:01Z",
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
        "statistics_params": DEFAULT_STATISTICS_PARAMS,
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
        carried = entry_from_payload(
            "seedbank.germination_by_taxon_mv",
            _payload(_matview_entry(max_age_days=30)),
        )

        assert carried.max_age_days == 30
        assert _payload(carried)["max_age_days"] == 30

    def test_an_entry_from_a_producer_that_recorded_none_stays_that_way(self) -> None:
        """The older-manifest path: absent must not become a key on the next run."""

        payload = _payload(_matview_entry())
        carried = entry_from_payload("seedbank.germination_by_taxon_mv", payload)

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
        carried = entry_from_payload(
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
        carried = entry_from_payload(
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

    def test_statistics_params_recorded_at_top_level(self) -> None:
        manifest = _manifest([_entry()])

        assert manifest["statistics_params"] == DEFAULT_STATISTICS_PARAMS

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

    def test_a_table_with_no_override_carries_no_statistics_params(self) -> None:
        assert "statistics_params" not in _payload(_entry())

    def test_a_tables_own_override_is_recorded(self) -> None:
        override = {"top_n_values": 50}
        assert _payload(_entry(statistics_params=override))["statistics_params"] == override

    def test_a_tables_override_survives_a_carry_forward(self) -> None:
        override = {"top_n_values": 50}
        carried = entry_from_payload(
            "seedbank.accession",
            _payload(_entry(statistics_params=override)),
        )

        assert carried.statistics_params == override
        assert _payload(carried)["statistics_params"] == override

    def test_an_entry_from_a_producer_that_recorded_none_stays_that_way(self) -> None:
        carried = entry_from_payload("seedbank.accession", _payload(_entry()))

        assert carried.statistics_params is None
        assert "statistics_params" not in _payload(carried)
