"""`numeric_row`: a numeric statement that fails is re-issued once without its mean and sum."""

from __future__ import annotations

import pytest

from dbprint.adapters.base import numeric_row
from dbprint.adapters.errors import QueryFailed
from dbprint.spec.rounding import UnrepresentableValue


class _Reads:
    def __init__(self, failure: Exception | None) -> None:
        self._failure = failure
        self.calls: list[bool] = []

    def __call__(self, aggregates: bool) -> tuple[int, int, int | None, int | None]:
        self.calls.append(aggregates)

        if aggregates and self._failure is not None:
            raise self._failure

        return (1, 2, 3, 4) if aggregates else (1, 2, None, None)


def test_a_statement_that_succeeds_is_read_once() -> None:
    reads = _Reads(None)

    assert numeric_row(reads) == ((1, 2, 3, 4), None)
    assert reads.calls == [True]


def test_a_failure_is_re_issued_without_the_totals_and_returned() -> None:
    failure = RuntimeError("Out of Range Error: Overflow in HUGEINT addition")
    reads = _Reads(failure)

    assert numeric_row(reads) == ((1, 2, None, None), failure)
    assert reads.calls == [True, False]


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(
            QueryFailed(RuntimeError("canceling statement"), "SELECT 1", timed_out=True),
            id="timed-out",
        ),
        pytest.param(UnrepresentableValue("12345-01-01"), id="unrepresentable"),
    ],
)
def test_a_timeout_or_an_unrepresentable_value_is_not_re_issued(failure: Exception) -> None:
    reads = _Reads(failure)

    with pytest.raises(type(failure)):
        numeric_row(reads)

    assert reads.calls == [True]
