"""Per-session worker threads for one connection's tables.

One single-thread executor per session, so a session sees one thread; one worker runs inline.
"""

from __future__ import annotations

import contextvars
from collections.abc import Callable, Iterable, Iterator, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, as_completed, wait
from typing import cast


class SessionPool[W]:
    """Workers bound one to a session, each unit run on the submitting thread's context copy."""

    def __init__(self, workers: Sequence[W]) -> None:
        self.workers = tuple(workers)
        self._executors = (
            [ThreadPoolExecutor(max_workers=1) for _ in self.workers]
            if len(self.workers) > 1
            else []
        )

    def free[U, R](
        self,
        units: Iterable[U],
        run: Callable[[W, U], R],
        *,
        on_submit: Callable[[U], None] | None = None,
        stop: Callable[[R], bool] | None = None,
    ) -> Iterator[tuple[U, int, R]]:
        """Start each unit, in order, on whichever worker is free; yield `(unit, worker, result)`
        as each finishes. Once `stop` holds for a result no unit starts, and those in flight finish.
        """

        pending = iter(units)

        if not self._executors:
            for unit in pending:
                if on_submit is not None:
                    on_submit(unit)

                result = run(self.workers[0], unit)

                yield unit, 0, result

                if stop is not None and stop(result):
                    return

            return

        free = list(range(len(self.workers)))
        in_flight: dict[Future[R], tuple[U, int]] = {}
        stopped = False

        try:
            while True:
                while free and not stopped:
                    try:
                        unit = next(pending)
                    except StopIteration:
                        stopped = True
                        break

                    worker = free.pop(0)

                    if on_submit is not None:
                        on_submit(unit)

                    in_flight[self._submit(worker, run, unit)] = (unit, worker)

                if not in_flight:
                    return

                done, _ = wait(in_flight, return_when=FIRST_COMPLETED)

                for future in done:
                    unit, worker = in_flight.pop(future)
                    free.append(worker)
                    result = future.result()

                    yield unit, worker, result

                    if stop is not None and stop(result):
                        stopped = True
        finally:
            wait(in_flight)

    def pinned[U, R](
        self,
        units: Iterable[tuple[int, U]],
        run: Callable[[W, U], R],
    ) -> Iterator[tuple[U, R]]:
        """Run each unit on the worker it names, queued behind that worker's earlier units; yield
        `(unit, result)` as each finishes.
        """

        if not self._executors:
            for worker, unit in units:
                yield unit, run(self.workers[worker], unit)

            return

        futures = {self._submit(worker, run, unit): unit for worker, unit in units}

        try:
            for future in as_completed(futures):
                yield futures[future], future.result()
        finally:
            for future in futures:
                future.cancel()

            wait(futures)

    def shutdown(self) -> None:
        """Stop every worker thread once its current unit ends; queued units never start."""

        for executor in self._executors:
            executor.shutdown(wait=True, cancel_futures=True)

    def _submit[U, R](self, worker: int, run: Callable[[W, U], R], unit: U) -> Future[R]:
        # A worker thread starts with an empty context; the trace tags live in the submitter's.
        context = contextvars.copy_context()

        future = self._executors[worker].submit(context.run, run, self.workers[worker], unit)

        return cast(Future[R], future)
