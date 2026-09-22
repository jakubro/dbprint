"""Selector unit tests - fnmatch semantics + intersect/union rules."""

from __future__ import annotations

from typing import ClassVar

from dbprint.config import expand, match


class TestMatch:
    def test_simple_match_in_scope(self) -> None:
        assert match("garden.seedbank.accession", ["garden.seedbank.accession"], []) is True

    def test_simple_non_match(self) -> None:
        assert (
            match("garden.seedbank.accession", ["garden.seedbank.germination_trial"], []) is False
        )

    def test_wildcard_matches_within_namespace(self) -> None:
        assert match("garden.seedbank.accession", ["garden.seedbank.*"], []) is True

    def test_wildcard_crosses_dots(self) -> None:
        # fnmatch * is greedy and crosses dot separators.
        assert match("garden.seedbank.accession", ["garden.*"], []) is True

    def test_question_mark_single_character(self) -> None:
        assert match("a.b.c", ["a.?.c"], []) is True
        assert match("a.bb.c", ["a.?.c"], []) is False

    def test_empty_include_excludes_everything(self) -> None:
        assert match("a.b.c", [], []) is False

    def test_exclude_overrides_include(self) -> None:
        assert match("a.b.c", ["a.*"], ["a.b.c"]) is False

    def test_exclude_pattern_wildcard(self) -> None:
        assert match("garden.seedbank.storage_reading", ["garden.*"], ["*.storage_*"]) is False

    def test_an_unfolded_fqn_still_does_not_match(self) -> None:
        # Only the pattern is folded; an adapter owes a lowercased FQN (ARCHITECTURE 6).
        assert match("Garden.Seedbank.Accession", ["garden.seedbank.*"], []) is False

    def test_an_uppercase_include_matches(self) -> None:
        assert match("garden.seedbank.accession", ["GARDEN.SEEDBANK.ACCESSION"], []) is True

    def test_a_mixed_case_include_matches(self) -> None:
        assert match("garden.seedbank.accession", ["Garden.Seedbank.Accession"], []) is True

    def test_an_uppercase_glob_matches(self) -> None:
        assert match("garden.seedbank.accession", ["GARDEN.*"], []) is True

    def test_an_uppercase_exclude_excludes(self) -> None:
        assert match("garden.seedbank.accession", ["*"], ["GARDEN.SEEDBANK.ACCESSION"]) is False

    def test_an_uppercase_glob_exclude_excludes(self) -> None:
        assert match("garden.seedbank.storage_reading", ["*"], ["*.STORAGE_*"]) is False

    def test_an_uppercase_character_class_matches(self) -> None:
        assert match("a.b.c", ["a.[A-Z].c"], []) is True


class TestExpand:
    FQNS: ClassVar[list[str]] = [
        "garden.seedbank.accession",
        "garden.seedbank.germination_trial",
        "garden.seedbank.storage_reading",
        "garden.fieldwork.collector",
        "fixture.staging.active_curators",
    ]

    def test_config_include_only(self) -> None:
        result = expand(self.FQNS, ["garden.seedbank.*"], [])
        assert result == [
            "garden.seedbank.accession",
            "garden.seedbank.germination_trial",
            "garden.seedbank.storage_reading",
        ]

    def test_config_include_plus_exclude(self) -> None:
        result = expand(self.FQNS, ["garden.*"], ["*.storage_*"])
        assert result == [
            "garden.seedbank.accession",
            "garden.seedbank.germination_trial",
            "garden.fieldwork.collector",
        ]

    def test_cli_include_narrows_config_include(self) -> None:
        # CLI include intersects with config include - narrowing only.
        result = expand(
            self.FQNS,
            config_include=["garden.*"],
            config_exclude=[],
            cli_include=["garden.seedbank.*"],
        )
        assert result == [
            "garden.seedbank.accession",
            "garden.seedbank.germination_trial",
            "garden.seedbank.storage_reading",
        ]

    def test_cli_include_cannot_widen_beyond_config(self) -> None:
        # fixture.* would match cli_include but config restricts to garden.*
        result = expand(
            self.FQNS,
            config_include=["garden.*"],
            config_exclude=[],
            cli_include=["fixture.*"],
        )
        assert result == []

    def test_cli_exclude_unions_with_config_exclude(self) -> None:
        result = expand(
            self.FQNS,
            config_include=["garden.*"],
            config_exclude=["*.storage_*"],
            cli_exclude=["garden.fieldwork.*"],
        )
        assert result == [
            "garden.seedbank.accession",
            "garden.seedbank.germination_trial",
        ]

    def test_preserves_input_order(self) -> None:
        scrambled = list(reversed(self.FQNS))
        result = expand(scrambled, ["*"], [])
        assert result == scrambled

    def test_empty_input(self) -> None:
        assert expand([], ["*"], []) == []
