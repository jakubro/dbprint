"""`SessionPool`: one thread per worker, inline with one, stop honoured, the caller's context carried."""

from __future__ import annotations

import threading
import time
from contextvars import ContextVar

from dbprint.engine.pool import SessionPool


_TAG: ContextVar[str] = ContextVar("pool_test_tag", default="")


def _slow(worker: str, unit: int) -> tuple[str, int, int]:
    time.sleep(0.01)

    return worker, unit, threading.get_ident()


class TestOneWorker:
    def test_units_run_inline_in_order(self) -> None:
        pool = SessionPool(["only"])
        seen = [(unit, result) for unit, _, result in pool.free(range(4), _slow)]

        assert [unit for unit, _ in seen] == [0, 1, 2, 3]
        assert {thread for _, (_, _, thread) in seen} == {threading.get_ident()}

    def test_stop_ends_the_run_after_the_unit_that_tripped_it(self) -> None:
        pool = SessionPool(["only"])
        done = [unit for unit, _, _ in pool.free(range(10), _slow, stop=lambda r: r[1] == 2)]

        assert done == [0, 1, 2]


class TestSeveralWorkers:
    def test_each_worker_is_driven_by_one_thread_of_its_own(self) -> None:
        pool = SessionPool(["a", "b", "c"])

        try:
            results = [result for _, _, result in pool.free(range(30), _slow)]
        finally:
            pool.shutdown()

        threads: dict[str, set[int]] = {}

        for worker, _, thread in results:
            threads.setdefault(worker, set()).add(thread)

        assert len(results) == 30
        assert set(threads) == {"a", "b", "c"}
        assert all(len(ids) == 1 for ids in threads.values())
        assert len({next(iter(ids)) for ids in threads.values()}) == 3

    def test_no_unit_starts_once_stop_holds(self) -> None:
        pool = SessionPool(["a", "b"])
        started: list[int] = []

        try:
            finished = [
                unit
                for unit, _, _ in pool.free(
                    range(20),
                    _slow,
                    on_submit=started.append,
                    stop=lambda r: r[1] == 0,
                )
            ]
        finally:
            pool.shutdown()

        assert sorted(finished) == sorted(started)
        assert len(started) < 20

    def test_a_pinned_unit_runs_on_the_worker_it_names(self) -> None:
        pool = SessionPool(["a", "b", "c"])

        try:
            placed = {
                unit: result[0]
                for unit, result in pool.pinned(
                    [(unit % 3, unit) for unit in range(9)],
                    _slow,
                )
            }
        finally:
            pool.shutdown()

        assert placed == {unit: "abc"[unit % 3] for unit in range(9)}

    def test_the_submitters_context_reaches_the_worker(self) -> None:
        pool = SessionPool(["a", "b"])
        token = _TAG.set("primary")

        try:
            tags = {result for _, _, result in pool.free(range(4), lambda _w, _u: _TAG.get())}
        finally:
            _TAG.reset(token)
            pool.shutdown()

        assert tags == {"primary"}
