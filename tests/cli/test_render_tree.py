"""Header emission on path change, right-anchored alignment, direction-correct truncation."""

from __future__ import annotations

import pytest

from dbprint.cli.rendering import tree


class TestHeaderPath:
    def test_two_part_fqn_groups_under_connection_and_schema(self) -> None:
        assert tree.header_path("acme", "seedbank.accession") == ("acme", "seedbank")
        assert tree.leaf_name("seedbank.accession") == "accession"

    def test_three_part_fqn_nests_database_then_schema(self) -> None:
        assert tree.header_path("wh", "fixture.fieldwork.sowing_trial") == (
            "wh",
            "fixture",
            "fieldwork",
        )
        assert tree.leaf_name("fixture.fieldwork.sowing_trial") == "sowing_trial"


class TestDivergentHeaders:
    def test_unchanged_path_emits_nothing(self) -> None:
        # consecutive siblings share (connection, schema) -> no header re-emit
        assert tree.divergent_headers(("acme", "seedbank"), ("acme", "seedbank")) == []

    def test_schema_change_emits_only_the_new_schema(self) -> None:
        assert tree.divergent_headers(("acme", "seedbank"), ("acme", "fixture")) == [(1, "fixture")]

    def test_connection_change_reemits_the_whole_path(self) -> None:
        assert tree.divergent_headers(("acme", "seedbank"), ("other", "seedbank")) == [
            (0, "other"),
            (1, "seedbank"),
        ]

    def test_initial_path_emits_every_level(self) -> None:
        assert tree.divergent_headers((), ("acme", "fixture", "fieldwork")) == [
            (0, "acme"),
            (1, "fixture"),
            (2, "fieldwork"),
        ]


class TestResolveCap:
    @pytest.mark.parametrize("width,expected", [(200, 120), (80, 80), (None, 120)])
    def test_caps_at_120(self, width: int | None, expected: int) -> None:
        assert tree.resolve_cap(width) == expected


class TestHeaderLine:
    def test_indented_two_spaces_per_depth(self) -> None:
        assert tree.header_line(0, "acme", cap=80) == "acme"
        assert tree.header_line(1, "seedbank", cap=80) == "  seedbank"
        assert tree.header_line(2, "fieldwork", cap=80) == "    fieldwork"


class TestBannerBox:
    def test_three_lines_rounded_and_cap_wide(self) -> None:
        top, middle, bottom = tree.banner_box("Validating", cap=20).split("\n")
        assert top == "╭" + "─" * 18 + "╮"
        assert bottom == "╰" + "─" * 18 + "╯"
        assert len(top) == len(middle) == len(bottom) == 20
        assert "Validating" in middle
        assert middle.startswith("│") and middle.endswith("│")

    def test_label_centred(self) -> None:
        _, middle, _ = tree.banner_box("Hi", cap=10).split("\n")
        assert middle == "│" + "Hi".center(8) + "│"

    def test_label_head_kept_when_narrower_than_cap_allows(self) -> None:
        _, middle, _ = tree.banner_box("Checking assertions", cap=12).split("\n")
        inner = middle[1:-1]
        assert len(inner) == 10
        assert inner.strip().endswith("...")
        assert inner.strip().startswith("Check")


class TestLeafAlignment:
    def test_rows_and_elapsed_align_across_mixed_widths(self) -> None:
        cap = 80
        cases = [("a", 0, 0), ("germination_trial", 4, 0), ("z" * 40, 42_724, 1200)]
        lines = [
            tree.leaf_metrics(
                2,
                name,
                cap=cap,
                rows=tree.rows_text(rc),
                elapsed=tree.duration_text(ms),
            )
            for name, rc, ms in cases
        ]

        # Every ok leaf fills the cap exactly, so rows/elapsed share a flush-right end column.
        assert all(len(line) == cap for line in lines)
        assert [
            line.endswith(tree.duration_text(ms)) for line, (_, _, ms) in zip(lines, cases)
        ] == [
            True,
            True,
            True,
        ]

    def test_long_leaf_is_tail_truncated_within_cap(self) -> None:
        cap = 60
        line = tree.leaf_metrics(
            2,
            "very_long_table_name_" + "z" * 30,
            cap=cap,
            rows=tree.rows_text(7),
            elapsed=tree.duration_text(300),
        )

        assert len(line) == cap
        assert line.endswith(tree.duration_text(300))
        assert "..." in line  # head dropped
        assert "z" in line  # distinguishing tail kept


class TestLeafWidthInvariant:
    """Below the floor where name and block cannot both fit, the block is shed - `cap` is an
    invariant, and the name takes the full remaining width.
    """

    def test_narrow_cap_sheds_the_block_and_stays_within_cap(self) -> None:
        cap = 40
        line = tree.leaf_metrics(
            2,
            "germination_trial",
            cap=cap,
            rows=tree.rows_text(120),
            elapsed=tree.duration_text(100),
        )

        assert len(line) <= cap
        assert "germination_trial" in line
        assert "rows" not in line  # the block was shed, not squeezed into an unreadable stub

    def test_wide_cap_keeps_the_block_unchanged(self) -> None:
        cap = 80
        line = tree.leaf_metrics(
            2,
            "germination_trial",
            cap=cap,
            rows=tree.rows_text(120),
            elapsed=tree.duration_text(100),
        )

        assert len(line) == cap
        assert "rows" in line

    def test_a_name_longer_than_the_shed_width_still_stays_within_cap(self) -> None:
        cap = 40
        line = tree.leaf_metrics(
            2,
            "z" * 60,
            cap=cap,
            rows=tree.rows_text(120),
            elapsed=tree.duration_text(100),
        )

        assert len(line) <= cap

    def test_indentation_alone_exceeding_cap_never_exceeds_it(self) -> None:
        """A pathologically narrow terminal where depth's own indent already outruns `cap` -
        the invariant holds even when there is no room for a name at all.
        """

        line = tree.leaf_metrics(
            5,
            "germination_trial",
            cap=6,
            rows=tree.rows_text(120),
            elapsed=tree.duration_text(100),
        )

        assert len(line) <= 6


class TestWarningLineWidthInvariant:
    def test_a_narrow_cap_never_exceeds_it(self) -> None:
        line = tree.warning_line(2, "no row-count estimate; rules do not apply", cap=8)

        assert len(line) <= 8

    def test_indentation_alone_exceeding_cap_never_exceeds_it(self) -> None:
        line = tree.warning_line(5, "no row-count estimate; rules do not apply", cap=6)

        assert len(line) <= 6


class TestHeaderLineWidthInvariant:
    def test_indentation_alone_exceeding_cap_never_exceeds_it(self) -> None:
        line = tree.header_line(5, "seedbank", cap=6)

        assert len(line) <= 6


class TestLeafNote:
    def test_skipped_note_is_right_anchored(self) -> None:
        line = tree.leaf_note(2, "stale_table", "(skipped)", cap=60)

        assert len(line) == 60
        assert line.endswith("(skipped)")
        assert line.startswith("    stale_table")


class TestLeafError:
    def test_error_is_head_kept_and_clipped(self) -> None:
        cap = 40
        line = tree.leaf_error(
            2,
            "curation_event",
            "NoneType has no attribute 'strip' plus trailing detail",
            cap=cap,
        )

        assert len(line) <= cap
        assert line.startswith("    curation_event")
        assert "NoneType" in line  # message start survives
        assert line.endswith("...")  # tail clipped


class TestWarningLine:
    def test_indented_one_level_past_the_leaf(self) -> None:
        line = tree.warning_line(2, "no row-count estimate", cap=60)

        assert line.startswith(" " * 6)  # (depth + 1) * _INDENT == 3 * 2
        assert "no row-count estimate" in line

    def test_head_kept_and_clipped(self) -> None:
        cap = 30
        line = tree.warning_line(2, "no row-count estimate; rules do not apply", cap=cap)

        assert len(line) <= cap
        assert "no row-count" in line  # subject survives
        assert line.endswith("...")
