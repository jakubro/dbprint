"""`driver.last_rows`: the rows of a cursor's last result set, read through PEP 249 `nextset`."""

from __future__ import annotations

from typing import Any

import pytest

from dbprint.adapters.driver import last_rows
from dbprint.adapters.errors import NoResultSet


class _Results:
    """A cursor holding several results; `None` stands for a statement with no result set."""

    def __init__(self, *results: list[tuple[Any, ...]] | None, clears: bool = False) -> None:
        self._results = list(results)
        self._index = 0
        self._clears = clears
        self.description: Any = self._description()

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._results[self._index] or [])

    def nextset(self) -> bool | None:
        if self._index + 1 >= len(self._results):
            if self._clears:
                self.description = None

            return None

        self._index += 1
        self.description = self._description()

        return True

    def _description(self) -> Any:
        return None if self._results[self._index] is None else [("n",)]


class _NoNextset:
    description = (("n",),)

    def fetchall(self) -> list[tuple[int]]:
        return [(7,)]


class _RefusedNextset(_NoNextset):
    def nextset(self) -> bool:
        raise NotImplementedError


class _LastHasNoColumns:
    def __init__(self, first: list[tuple[Any, ...]], last_description: Any) -> None:
        self._first = first
        self._last_description = last_description
        self._advanced = False
        self.description: Any = [("n",)]

    def fetchall(self) -> list[tuple[Any, ...]]:
        return [] if self._advanced else self._first

    def nextset(self) -> bool | None:
        if self._advanced:
            return None

        self._advanced = True
        self.description = self._last_description

        return True


def test_one_result() -> None:
    assert last_rows(_Results([(1,), (2,)])) == [(1,), (2,)]


def test_the_last_of_three_results() -> None:
    assert last_rows(_Results([(0,)], [], [(3,)])) == [(3,)]


@pytest.mark.parametrize("cursor", [_NoNextset(), _RefusedNextset()], ids=["absent", "refused"])
def test_a_cursor_without_nextset_has_one_result(cursor: Any) -> None:
    assert last_rows(cursor) == [(7,)]


def test_a_final_nextset_that_clears_the_description_keeps_the_last_rows() -> None:
    assert last_rows(_Results([(0,)], [(3,)], clears=True)) == [(3,)]


@pytest.mark.parametrize("description", [None, []], ids=["none", "empty"])
def test_a_last_statement_with_no_columns_is_refused(description: Any) -> None:
    with pytest.raises(NoResultSet, match="the last statement returned no result set"):
        last_rows(_LastHasNoColumns([(0,)], description))
