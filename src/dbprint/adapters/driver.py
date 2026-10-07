"""The DB-API surface every SQL adapter reads through: the cursor, the traced statement call, the
host credentials, and the session a cursor factory opens. An engine supplies its driver call and
its failure wording.
"""

from __future__ import annotations

import importlib
import time
from collections.abc import Callable, Mapping
from dataclasses import MISSING, dataclass, fields
from logging import Logger
from types import MappingProxyType, ModuleType
from typing import Any, ClassVar, Protocol, Self

from dbprint.config.duration import format_duration_seconds
from . import trace_context
from .errors import QueryFailed


type CursorFactory[P] = Callable[[P], Any]


class Cursor(Protocol):
    """DB-API-compatible cursor surface used by the adapters."""

    def execute(self, sql: str, params: Any = ...) -> Any: ...

    def fetchall(self) -> list[Any]: ...

    def fetchone(self) -> Any: ...

    def close(self) -> None: ...


def traced(
    logger: Logger,
    is_timeout: Callable[[BaseException], bool],
    run: Callable[[], Any],
    sql: str,
    params: Any,
) -> Any:
    """`run()`'s result, DEBUG-traced with `sql` and `params` as a pair.

    Raises `QueryFailed` wrapping the driver's error, marked `timed_out` where `is_timeout` says so.
    """

    started = time.monotonic()

    try:
        result = run()
    except Exception as exc:
        failure = QueryFailed(exc, sql, params, timed_out=is_timeout(exc))
        trace_context.log_failure(logger, started, failure)

        raise failure from exc

    trace_context.log_success(logger, started, sql, params, getattr(result, "rowcount", None))

    return result


def execute(
    logger: Logger,
    is_timeout: Callable[[BaseException], bool],
    cursor: Cursor,
    sql: str,
    params: Any = None,
) -> Cursor:
    """Run `sql` on `cursor` through `traced` and return the cursor; no params means none bound."""

    def run() -> Cursor:
        if params is None:
            cursor.execute(sql)
        else:
            cursor.execute(sql, params)

        return cursor

    return traced(logger, is_timeout, run, sql, params)


def import_extra(module: str, package: str, extra: str, error: type[Exception]) -> ModuleType:
    """`module`, imported lazily so a base install never pays for it; `error` names the extra."""

    try:
        return importlib.import_module(module)
    except ImportError as exc:
        raise error(
            f"{package} is not installed. Install dbprint with the [{extra}] extra: "
            f"`pip install dbprint[{extra}]`.",
        ) from exc


def connect_failure(vendor: str, params: ServerParams, exc: Exception) -> str:
    """How every server adapter words a connection it could not open."""

    where = f"{params.host}:{params.port}"
    where += f"/{params.database}" if params.database is not None else ""

    return f"could not connect to {vendor} at {where} as {params.user!r}: {exc}"


@dataclass(frozen=True)
class ServerParams:
    """Resolved host credentials; a subclass names `error` and the keys its server defaults."""

    error: ClassVar[type[Exception]]
    defaults: ClassVar[Mapping[str, str]] = MappingProxyType({})

    host: str
    port: int
    user: str
    password: str
    database: str | None = None
    statement_timeout: int | None = None

    @classmethod
    def required_keys(cls) -> tuple[str, ...]:
        """The credential keys with no default, in the order a resolver asks for them."""

        return tuple(
            f.name for f in fields(cls) if f.default is MISSING and f.name not in cls.defaults
        )

    @classmethod
    def optional_keys(cls) -> tuple[str, ...]:
        """`database`, then every key the server defaults."""

        return ("database", *cls.defaults)

    @classmethod
    def from_credentials(cls, creds: dict[str, str], statement_timeout: int | None = None) -> Self:
        merged = {**cls.defaults, **creds}

        try:
            return cls(
                host=merged["host"],
                port=int(merged["port"]),
                database=merged.get("database"),
                user=merged["user"],
                password=merged["password"],
                statement_timeout=statement_timeout,
            )
        except KeyError as exc:
            raise cls.error(f"missing required credential key: {exc.args[0]!r}") from exc
        except ValueError as exc:
            raise cls.error(f"invalid port {merged.get('port')!r}: {exc}") from exc


class FactoryConnection:
    """A session whose cursor a factory opens - the seam a test substrate is injected through.

    A subclass names `error`, its default factory and open-failure text; `timeout_ceiling` refuses.
    """

    error: ClassVar[type[Exception]]
    vendor: ClassVar[str]
    timeout_ceiling: ClassVar[int | None] = None

    def __init__(self, params: Any, cursor_factory: CursorFactory[Any] | None = None) -> None:
        self.params = params
        self._factory = cursor_factory or self._default_factory
        self._cursor: Any | None = None

    def sibling(self) -> Self:
        """An unopened connection with the same parameters and cursor factory."""

        return type(self)(self.params, self._factory)

    def open(self) -> None:
        limit = self.params.statement_timeout

        if self.timeout_ceiling is not None and limit is not None and limit > self.timeout_ceiling:
            ceiling = format_duration_seconds(self.timeout_ceiling)

            raise self.error(
                f"statement_timeout {format_duration_seconds(limit)} exceeds {self.vendor}'s "
                f"ceiling of {ceiling} ({self.timeout_ceiling} seconds).",
            )

        try:
            cursor = self._factory(self.params)
        except self.error:
            raise
        except Exception as exc:
            raise self.error(self._open_failure(exc)) from exc

        self._cursor = self._opened(cursor)

    def close(self) -> None:
        if self._cursor is not None:
            try:
                self._cursor.close()
            except Exception:  # noqa: BLE001, S110 - close-time failure is uninteresting
                pass

            self._cursor = None

    def is_open(self) -> bool:
        return self._cursor is not None

    @property
    def cursor(self) -> Cursor:
        if self._cursor is None:
            raise self.error("connection is not open; call connect() first")

        return self._cursor

    def _default_factory(self, params: Any) -> Any:
        raise NotImplementedError

    def _open_failure(self, exc: Exception) -> str:
        raise NotImplementedError

    def _opened(self, cursor: Any, /) -> Any:
        return cursor
