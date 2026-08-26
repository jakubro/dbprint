"""The docs site's Flask app: routes, template filters, app factory.

The only module in this package that imports Flask. Every request re-reads the print from
disk, so a page reflects the latest `generate`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import markdown
from flask import Flask, abort, render_template
from jinja2 import ChainableUndefined
from markupsafe import Markup

from dbprint.config import ConnectionConfig
from . import catalogue, view


NBSP = chr(0xA0)


def create_app(connections: list[ConnectionConfig]) -> Flask:
    """Build the docs Flask app over `connections` - the CLI's already-resolved set."""

    app = Flask(__name__)
    app.jinja_env.undefined = ChainableUndefined  # absent keys render blank
    app.config["DOCS_CONNECTIONS"] = list(connections)

    _register_filters(app)
    _register_routes(app)

    return app


def _register_routes(app: Flask) -> None:
    @app.get("/")
    def index() -> str:
        """Render every connection's table list."""

        conns = catalogue.load_connections(app.config["DOCS_CONNECTIONS"])

        return render_template(
            "index.html",
            connections=view.build_index_view(conns),
            **_sidebar_context(conns),
        )

    @app.get("/s/<conn>/<schema>")
    def schema(conn: str, schema: str) -> str:
        """Render every table in one schema and their intra-schema relationships."""

        conns = catalogue.load_connections(app.config["DOCS_CONNECTIONS"])
        found = catalogue.find_connection(conns, conn)
        detail = view.build_schema_view(found, schema) if found else None

        if detail is None:
            abort(404)

        return render_template(
            "schema.html",
            conn=conn,
            schema=schema,
            **_sidebar_context(conns),
            **detail,
        )

    @app.get("/t/<conn>/<table>")
    def table(conn: str, table: str) -> str:
        """Render one table's artifacts."""

        conns = catalogue.load_connections(app.config["DOCS_CONNECTIONS"])
        found = catalogue.find_connection(conns, conn)

        if found is None:
            abort(404)

        artifacts = catalogue.load_table(found, table)

        if artifacts is None:
            abort(404)

        page = view.build_table_view(found, artifacts)

        return render_template("table.html", conn=conn, **_sidebar_context(conns), **page)


def _sidebar_context(connections: list[catalogue.PrintConnection]) -> dict[str, Any]:
    """Nav tree and each connection's on-disk root."""

    return {
        "nav": catalogue.nav_tree(connections),
        "conn_roots": {c.name: str(c.root) for c in connections},
    }


def _register_filters(app: Flask) -> None:
    app.add_template_filter(_render_markdown, "md")
    app.add_template_filter(_non_breaking, "nbsp")
    app.add_template_filter(_human_number, "human")
    app.add_template_filter(_pretty_datetime, "dt")
    app.add_template_filter(_relative_time, "relative")


def _render_markdown(text: str | None) -> Markup | str:
    """Render Markdown text as safe HTML."""

    return Markup(markdown.markdown(text, extensions=["tables", "fenced_code"])) if text else ""


def _non_breaking(text: str | None) -> str | None:
    """Replace underscores with a non-breaking space, so a long name does not wrap mid-word."""

    return text.replace("_", NBSP) if text else text


def _human_number(n: Any) -> str:
    """Format a count with K/M/B/T suffixes; blank for anything that is not one.

    A plain view's manifest entry carries no `row_count` (SPEC 1.4), so this also absorbs
    Jinja's `Undefined`, whose `float()` raises `UndefinedError` rather than `TypeError`.
    """

    try:
        n = float(n)
    except Exception:  # noqa: BLE001 - degrade any non-numeric input, incl. jinja2.Undefined
        return ""

    units = [("T", 1e12), ("B", 1e9), ("M", 1e6), ("K", 1e3)]

    for i, (unit, div) in enumerate(units):
        if abs(n) < div:
            continue

        value = round(n / div, 1)

        if abs(value) >= 1000 and i > 0:  # e.g. 999999 rounds to 1000K, bump to 1M
            unit, div = units[i - 1]
            value = round(n / div, 1)

        return f"{value:.1f}".rstrip("0").rstrip(".") + unit

    return str(int(n))


def _pretty_datetime(value: Any) -> Any:
    """Render an ISO date or timestamp string in a readable form."""

    if not isinstance(value, str):
        return value

    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return value  # unrepresentable extreme date

    if "T" not in value:
        return parsed.strftime("%b %d, %Y")

    pretty = parsed.strftime("%b %d, %Y %H:%M:%S")
    offset = parsed.utcoffset()

    if offset is not None and offset.total_seconds() == 0:
        pretty += " UTC"

    return pretty


def _relative_time(value: Any) -> Any:
    """Render an ISO timestamp as '<n> <unit>(s) ago'."""

    if not isinstance(value, str):
        return value

    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return value  # unrepresentable extreme date

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)

    seconds = max(0.0, (datetime.now(UTC) - parsed).total_seconds())

    for unit, size in (
        ("year", 365.25 * 86400),
        ("month", 30.44 * 86400),
        ("day", 86400),
        ("hour", 3600),
    ):
        if seconds >= size:
            n = round(seconds / size)

            return f"{n} {unit}{'s' if n != 1 else ''} ago"

    n = max(1, round(seconds / 60))

    return f"{n} minute{'s' if n != 1 else ''} ago"
