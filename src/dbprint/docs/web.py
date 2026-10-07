"""The docs site's Flask app: routes, template filters, app factory.

The only module in this package that imports Flask. Every request re-reads the print from
disk, so a page reflects the latest `generate`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import markdown
from flask import Flask, abort, render_template
from jinja2 import ChainableUndefined
from markupsafe import Markup

from dbprint.config import ConnectionConfig
from dbprint.spec.value_text import spell_number, spell_percent
from . import catalogue, view


NBSP = chr(0xA0)
_CONTEXT_INERT_PATTERNS = (
    "html",
    "reference",
    "link",
    "image_link",
    "image_reference",
    "short_reference",
    "short_image_ref",
    "autolink",
    "automail",
)


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
    app.add_template_filter(_render_context, "context_md")
    app.add_template_filter(_non_breaking, "nbsp")
    app.add_template_filter(_number, "number")
    app.add_template_filter(_percent, "percent")
    app.add_template_filter(_pretty_datetime, "dt")
    app.add_template_filter(_relative_time, "relative")


def _render_markdown(text: str | None) -> Markup | str:
    """Render Markdown text as safe HTML."""

    return Markup(markdown.markdown(text, extensions=["tables", "fenced_code"])) if text else ""


def _render_context(text: str) -> Markup:
    """Render a context fragment as HTML, its line breaks kept, raw HTML and links shown as text.

    The fragment quotes database values: a stored `<script>` or `[x](javascript:...)` stays text.
    """

    renderer = markdown.Markdown(extensions=["tables", "fenced_code", "nl2br"])
    renderer.preprocessors.deregister("html_block")

    for pattern in _CONTEXT_INERT_PATTERNS:
        renderer.inlinePatterns.deregister(pattern)

    return Markup(renderer.convert(text))


def _non_breaking(text: str | None) -> str | None:
    """Replace underscores with a non-breaking space, so a long name does not wrap mid-word."""

    return text.replace("_", NBSP) if text else text


def _number(value: Any) -> str:
    """A statistic or count as the artifact spells it; blank for anything that is not a number.

    Jinja's `Undefined` included: a plain view's entry has no `row_count` (SPEC 1.4).
    """

    return spell_number(value) if _is_number(value) else ""


def _percent(ratio: Any) -> str:
    """A share as a percentage that never rounds a partial one onto 0% or 100%; blank otherwise."""

    return spell_percent(ratio) if _is_number(ratio) else ""


def _pretty_datetime(value: Any) -> Any:
    """Render an ISO date or timestamp string in a readable form, and a number as the artifact does."""

    if _is_number(value):
        return spell_number(value)

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


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float | Decimal) and not isinstance(value, bool)
