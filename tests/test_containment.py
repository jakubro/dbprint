"""`_containment.py` - the comparison behind the guard that the run wrote only to /tmp."""

from __future__ import annotations

from pathlib import Path

from tests import _containment


class TestEscaped:
    def test_an_unchanged_root_reports_nothing(self) -> None:
        snap = {"/var/lib": frozenset({"apt", "dpkg"})}

        assert _containment.escaped(snap, snap) == {}

    def test_a_new_entry_is_named_under_its_root(self) -> None:
        before = {"/var/lib": frozenset({"apt"})}
        after = {"/var/lib": frozenset({"apt", "dbprint-test-9f2c"})}

        assert _containment.escaped(before, after) == {"/var/lib": ["dbprint-test-9f2c"]}

    def test_every_new_entry_under_one_root_is_listed_sorted(self) -> None:
        before = {"/root": frozenset()}
        after = {"/root": frozenset({"zeta", "alpha"})}

        assert _containment.escaped(before, after) == {"/root": ["alpha", "zeta"]}

    def test_a_removed_entry_is_not_reported(self) -> None:
        before = {"/root": frozenset({"alpha", "beta"})}
        after = {"/root": frozenset({"alpha"})}

        assert _containment.escaped(before, after) == {}

    def test_a_root_absent_before_reports_all_of_its_entries(self) -> None:
        after = {"/var/lib/postgresql": frozenset({"cluster"})}

        assert _containment.escaped({}, after) == {"/var/lib/postgresql": ["cluster"]}

    def test_several_roots_are_reported_together(self) -> None:
        before = {"/var/lib": frozenset(), "/root": frozenset()}
        after = {"/var/lib": frozenset({"one"}), "/root": frozenset({"two"})}

        assert _containment.escaped(before, after) == {"/var/lib": ["one"], "/root": ["two"]}


class TestSnapshot:
    def test_it_reads_the_entry_names_a_root_holds(self, tmp_path: Path) -> None:
        (tmp_path / "alpha").mkdir()
        (tmp_path / "beta").write_text("")

        assert _containment.snapshot((tmp_path,)) == {str(tmp_path): frozenset({"alpha", "beta"})}

    def test_a_missing_root_is_omitted_rather_than_recorded_empty(self, tmp_path: Path) -> None:
        assert _containment.snapshot((tmp_path / "absent",)) == {}

    def test_a_repeated_root_is_read_once(self, tmp_path: Path) -> None:
        (tmp_path / "alpha").mkdir()

        assert _containment.snapshot((tmp_path, tmp_path)) == {
            str(tmp_path): frozenset({"alpha"}),
        }

    def test_a_snapshot_pair_around_a_new_entry_names_it(self, tmp_path: Path) -> None:
        before = _containment.snapshot((tmp_path,))
        (tmp_path / "stray").mkdir()

        assert _containment.escaped(before, _containment.snapshot((tmp_path,))) == {
            str(tmp_path): ["stray"],
        }


class TestSuiteEntries:
    def test_a_root_holding_none_reports_nothing(self, tmp_path: Path) -> None:
        (tmp_path / "logrotate").mkdir()

        assert _containment.suite_entries((tmp_path,), "dbprint") == []

    def test_a_cluster_directory_left_behind_is_named(self, tmp_path: Path) -> None:
        (tmp_path / "dbprint-test-postgres-9f2c").mkdir()
        (tmp_path / "mysql").mkdir()

        assert _containment.suite_entries((tmp_path,), "dbprint") == [
            str(tmp_path / "dbprint-test-postgres-9f2c"),
        ]

    def test_entries_across_roots_are_reported_together_and_sorted(self, tmp_path: Path) -> None:
        first, second = tmp_path / "one", tmp_path / "two"
        first.mkdir()
        second.mkdir()
        (first / "dbprint-b").mkdir()
        (second / "dbprint-a").mkdir()

        assert _containment.suite_entries((first, second), "dbprint") == [
            str(first / "dbprint-b"),
            str(second / "dbprint-a"),
        ]

    def test_a_missing_root_is_skipped(self, tmp_path: Path) -> None:
        assert _containment.suite_entries((tmp_path / "absent",), "dbprint") == []
