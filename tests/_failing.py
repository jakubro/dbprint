"""A mock adapter whose named tables fail Phase A, for every surface reading a partial run."""

from __future__ import annotations

from dbprint.adapters import MockAdapter, MockTable


class Failing(MockAdapter):
    def __init__(self, fixture: dict[str, MockTable], failing: str, sink: str) -> None:
        super().__init__(fixture)
        self._failing = failing
        self._sink = sink

    def estimate_row_count(self, fqn: str) -> int | None:
        if fqn == self._failing and self._sink == "estimate":
            raise RuntimeError("simulated catalog failure")

        return super().estimate_row_count(fqn)

    def extract_ddl(self, fqn: str) -> str:
        if fqn == self._failing and self._sink == "extraction":
            raise RuntimeError("simulated extraction failure")

        return super().extract_ddl(fqn)
