"""The claim mechanism behind the private-root guard's one-violation-one-failure attribution.

A private root is one filesystem shared by every worker and test, so `_claim`/`_unclaimed_problems`
are what make an escape report exactly once. Exercised against a `tmp_path`-redirected claims dir,
never a real one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests import conftest


@pytest.fixture(autouse=True)
def _claims_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the claims directory so a test never touches the real, run-shared one."""

    claims_dir = tmp_path / "claims"
    monkeypatch.setattr(conftest, "_CONTAINMENT_CLAIMS_DIR", claims_dir)

    return claims_dir


class TestClaim:
    def test_the_first_caller_claims_it(self) -> None:
        assert conftest._claim("appeared::/root::.foo") is True

    def test_a_second_caller_for_the_same_key_does_not(self) -> None:
        conftest._claim("appeared::/root::.foo")

        assert conftest._claim("appeared::/root::.foo") is False

    def test_two_different_keys_are_independent(self) -> None:
        assert conftest._claim("appeared::/root::.foo") is True
        assert conftest._claim("appeared::/root::.bar") is True

    def test_a_key_carrying_path_separators_does_not_escape_the_claims_dir(
        self,
        _claims_dir: Path,
    ) -> None:
        assert conftest._claim("appeared::/root::../../etc/passwd") is True

        created = list(_claims_dir.iterdir())
        assert len(created) == 1
        assert "/" not in created[0].name


class TestUnclaimedProblems:
    def test_a_new_entry_is_reported_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        before = {"/root": frozenset()}
        monkeypatch.setattr(conftest._containment, "snapshot", lambda: {"/root": frozenset({".x"})})
        monkeypatch.setattr(conftest._containment, "suite_entries", list)

        first = conftest._unclaimed_problems(before)
        second = conftest._unclaimed_problems(before)

        assert first == ["new entries under a private root: {'/root': ['.x']}"]
        assert second == []

    def test_a_stray_entry_is_reported_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(conftest._containment, "snapshot", dict)
        monkeypatch.setattr(conftest._containment, "suite_entries", lambda: ["/var/lib/dbprint-x"])

        first = conftest._unclaimed_problems({})
        second = conftest._unclaimed_problems({})

        assert first == ["suite-named entries outside the scratch tree: ['/var/lib/dbprint-x']"]
        assert second == []

    def test_no_violation_reports_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(conftest._containment, "snapshot", dict)
        monkeypatch.setattr(conftest._containment, "suite_entries", list)

        assert conftest._unclaimed_problems({}) == []

    def test_two_distinct_entries_are_each_reported(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Claim-based dedup must not over-suppress unrelated violations."""

        before = {"/root": frozenset()}
        monkeypatch.setattr(
            conftest._containment,
            "snapshot",
            lambda: {"/root": frozenset({".x", ".y"})},
        )
        monkeypatch.setattr(conftest._containment, "suite_entries", list)

        assert conftest._unclaimed_problems(before) == [
            "new entries under a private root: {'/root': ['.x', '.y']}",
        ]
