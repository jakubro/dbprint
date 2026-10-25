"""Ctrl-C as run state: recorded once by the CLI's handler, read by the statement seam and the commit.

A driver may report the interrupt as an ordinary error, so the run state is what ends the run.
"""

from __future__ import annotations

import signal
import threading
from collections.abc import Generator
from contextlib import contextmanager
from types import FrameType


class Interrupted(KeyboardInterrupt):
    """The run was interrupted; not an `Exception`, so no broad catch can degrade it."""


_REQUESTED = threading.Event()


@contextmanager
def handling() -> Generator[None]:
    """Record a Ctrl-C for the block, which then ends in `Interrupted` however it ended.

    On a thread other than the main one it does nothing, since only the main thread takes signals.
    """

    if threading.current_thread() is not threading.main_thread():
        yield

        return

    _REQUESTED.clear()
    previous = signal.signal(signal.SIGINT, _record)

    try:
        yield
    except Exception as exc:
        if _REQUESTED.is_set():
            raise Interrupted from exc

        raise
    finally:
        signal.signal(signal.SIGINT, previous)
        interrupted = _REQUESTED.is_set()
        _REQUESTED.clear()

    if interrupted:
        raise Interrupted


def requested() -> bool:
    """Whether the run in progress has been interrupted."""

    return _REQUESTED.is_set()


def caused_by_interrupt(exc: BaseException) -> bool:
    """Whether `exc` stands for an interrupt: the run was interrupted, or its cause chain holds one."""

    if _REQUESTED.is_set():
        return True

    seen: set[int] = set()
    current: BaseException | None = exc

    while current is not None and id(current) not in seen:
        if isinstance(current, KeyboardInterrupt):
            return True

        seen.add(id(current))
        current = current.__cause__ or current.__context__

    return False


def raise_if_requested() -> None:
    """Raise `Interrupted` when the run in progress has been interrupted."""

    if _REQUESTED.is_set():
        raise Interrupted


def _record(signum: int, frame: FrameType | None) -> None:
    del signum, frame
    _REQUESTED.set()

    raise KeyboardInterrupt
