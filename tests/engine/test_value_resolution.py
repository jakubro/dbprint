"""`resolve` answers a phrase from one column's published values (MCP.md 4.7)."""

from __future__ import annotations

from typing import Any

from dbprint.engine.value_resolution import DOMAIN_LIMIT, fold, resolve, spelling_groups


RANK_ENTRIES: list[Any] = [
    {"value": "species", "count": 288},
    {"value": "genus", "count": 8},
    {"value": "family", "count": 4},
]
MEDIUM_NOTES = {"control": "No growth medium: a no-treatment control group"}


class TestFold:
    def test_case_and_surrounding_space_fold_away(self) -> None:
        assert fold("  ACTIVE ") == "active"

    def test_inner_punctuation_and_accents_are_kept(self) -> None:
        """The key is the one `normalized_cardinality` is measured with, and no more."""

        assert fold("Credit-Card") == "credit-card"
        assert fold("José") == "josé"


class TestStored:
    def test_an_exact_value_resolves_to_itself(self) -> None:
        reply = resolve("genus", RANK_ENTRIES, {}, coverage=1.0)

        assert reply["match"] == "stored"
        assert reply["spellings"] == [{"value": "genus", "count": 8}]

    def test_another_casing_resolves_to_the_stored_spelling(self) -> None:
        reply = resolve("Genus", RANK_ENTRIES, {}, coverage=1.0)

        assert reply["spellings"][0]["value"] == "genus"

    def test_the_note_rides_along_with_the_value(self) -> None:
        reply = resolve("control", [{"value": "control", "count": 12}], MEDIUM_NOTES, coverage=1.0)

        assert reply["spellings"][0]["note"] == MEDIUM_NOTES["control"]


class TestDefinition:
    def test_a_phrase_from_a_note_finds_the_value_it_defines(self) -> None:
        reply = resolve(
            "no-treatment control group",
            [{"value": "control", "count": 12}, {"value": "peat", "count": 30}],
            MEDIUM_NOTES,
            coverage=1.0,
        )

        assert reply["match"] == "definition"
        assert [c["value"] for c in reply["candidates"]] == ["control"]

    def test_a_fragment_that_is_not_a_whole_phrase_does_not_match(self) -> None:
        """`ontrol` inside `control` is not a definition - the match is on word boundaries."""

        reply = resolve(
            "ontrol grou",
            [{"value": "control", "count": 12}],
            MEDIUM_NOTES,
            coverage=1.0,
        )

        assert reply["match"] != "definition"


class TestNearest:
    def test_a_misspelling_ranks_the_value_it_resembles_first(self) -> None:
        reply = resolve("specie", RANK_ENTRIES, {}, coverage=1.0)

        assert reply["match"] == "nearest"
        assert reply["candidates"][0]["value"] == "species"
        assert reply["candidates"][0]["score"] > 0

    def test_a_phrase_resembling_nothing_matches_nothing(self) -> None:
        reply = resolve("zzzzzzzzzz", RANK_ENTRIES, {}, coverage=1.0)

        assert reply["match"] == "none"
        assert "candidates" not in reply

    def test_at_most_five_candidates_come_back(self) -> None:
        entries: list[Any] = [{"value": f"rank-{i:02d}", "count": 1} for i in range(20)]

        reply = resolve("rank-07", entries, {}, coverage=1.0)

        assert reply["match"] in {"stored", "nearest"}
        assert len(reply.get("candidates", [])) <= 5


class TestWhatEveryReplyCarries:
    def test_an_exhaustive_short_list_rides_along_whole(self) -> None:
        reply = resolve("genus", RANK_ENTRIES, {}, coverage=1.0)

        assert [entry["value"] for entry in reply["domain"]] == ["species", "genus", "family"]
        assert reply["listed"] == 3

    def test_a_list_past_the_domain_limit_is_not_carried(self) -> None:
        entries: list[Any] = [{"value": f"v{i}", "count": 1} for i in range(DOMAIN_LIMIT + 1)]

        reply = resolve("v1", entries, {}, coverage=1.0)

        assert "domain" not in reply

    def test_a_sampled_list_carries_the_caveat_and_no_domain(self) -> None:
        reply = resolve("genus", RANK_ENTRIES, {}, coverage=0.4)

        assert "not evidence" in reply["sample_caveat"]
        assert "domain" not in reply

    def test_an_unavailable_column_answers_with_its_reason(self) -> None:
        reply = resolve("genus", [], {}, coverage=None, unavailable_reason="column is redacted")

        assert reply == {
            "match": "unavailable",
            "reason": "column is redacted",
            "coverage": None,
            "exhaustive": False,
            "listed": 0,
        }


class TestSpellingGroups:
    """SPEC 2.2.4: two entries are one category when their values fold to one key."""

    def test_the_most_frequent_spelling_is_the_canonical_one(self) -> None:
        assert spelling_groups([("Active", 90), ("ACTIVE", 10)]) == {1: "Active"}

    def test_the_canonical_member_can_sit_anywhere_in_the_list(self) -> None:
        assert spelling_groups([("ACTIVE", 10), ("Active", 90)]) == {0: "Active"}

    def test_an_ungrouped_value_is_absent_from_the_map(self) -> None:
        assert spelling_groups([("Active", 90), ("Retired", 50)]) == {}

    def test_a_tie_goes_to_the_earlier_entry(self) -> None:
        """The list's own order is already fixed by SPEC 2.2.4, so the tie needs no new rule."""

        assert spelling_groups([("Active", 50), ("ACTIVE", 50)]) == {1: "Active"}

    def test_three_spellings_all_name_the_same_canonical(self) -> None:
        groups = spelling_groups([("Active", 90), ("ACTIVE", 10), ("active", 5)])

        assert groups == {1: "Active", 2: "Active"}

    def test_non_string_values_never_group(self) -> None:
        assert spelling_groups([(1, 10), (1.0, 5), (True, 2)]) == {}


class TestTheAnswerIsTheWholeGroup:
    """One spelling where the column holds several is a predicate that misses rows."""

    def test_a_stored_match_carries_every_spelling(self) -> None:
        entries: list[Any] = [
            {"value": "sand tray", "count": 151},
            {"value": "Sand Tray", "count": 25, "spelling_of": "sand tray"},
        ]

        reply = resolve("SAND TRAY", entries, {}, coverage=1.0)

        assert reply["match"] == "stored"
        assert [s["value"] for s in reply["spellings"]] == ["sand tray", "Sand Tray"]

    def test_an_ungrouped_value_still_answers_with_one(self) -> None:
        reply = resolve("genus", RANK_ENTRIES, {}, coverage=1.0)

        assert [s["value"] for s in reply["spellings"]] == ["genus"]


class TestTheFoldMatchesWhatTheProducerMeasures:
    """SPEC 2.2.4: the key is `LOWER(TRIM(...))`, which `casefold` is not."""

    def test_the_sharp_s_is_not_folded_away(self) -> None:
        assert fold("Straße") != fold("STRASSE")

    def test_only_spaces_are_trimmed(self) -> None:
        assert fold("\tactive") != fold("active")
        assert fold("  active ") == fold("active")


class TestADefinitionMatchesOnWordBoundaries:
    def test_a_note_inside_a_longer_word_does_not_match(self) -> None:
        reply = resolve(
            "inactive members",
            [{"value": "A", "count": 5}],
            {"A": "active"},
            coverage=1.0,
        )

        assert reply["match"] != "definition"

    def test_a_note_that_is_a_whole_phrase_of_the_text_matches(self) -> None:
        reply = resolve(
            "the active ones",
            [{"value": "A", "count": 5}],
            {"A": "active"},
            coverage=1.0,
        )

        assert reply["match"] == "definition"


class TestExhaustivenessIsStatedOnEveryReply:
    """`coverage: null` cannot tell a whole column from a sample; `exhaustive` can."""

    def test_the_flag_defaults_to_the_coverage(self) -> None:
        assert resolve("genus", RANK_ENTRIES, {}, coverage=1.0)["exhaustive"] is True
        assert resolve("genus", RANK_ENTRIES, {}, coverage=0.4)["exhaustive"] is False

    def test_an_exhaustive_list_without_a_coverage_carries_the_domain_and_no_caveat(self) -> None:
        entries: list[Any] = [{"value": 2018, "count": 1}, {"value": 2019, "count": 2}]

        reply = resolve("2019", entries, {}, coverage=None, exhaustive=True)

        assert reply["match"] == "stored"
        assert reply["exhaustive"] is True
        assert [e["value"] for e in reply["domain"]] == [2018, 2019]
        assert "sample_caveat" not in reply

    def test_an_unavailable_reply_states_it_too(self) -> None:
        reply = resolve("x", [], {}, coverage=None, unavailable_reason="no values")

        assert reply["exhaustive"] is False
