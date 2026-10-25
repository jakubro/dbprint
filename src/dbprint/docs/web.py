"""The docs site's Flask app: routes, template filters, app factory.

The only module in this package that imports Flask. Every request re-reads the print from
disk, so a page reflects the latest `generate`.
"""

from __future__ import annotations

import html
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast
from urllib.parse import urlsplit
from xml.etree.ElementTree import Element

import markdown
from flask import Flask, abort, render_template, request
from jinja2 import ChainableUndefined
from markdown.treeprocessors import Treeprocessor
from markdown.util import AMP_SUBSTITUTE
from markupsafe import Markup

from dbprint.config import ConnectionConfig
from dbprint.spec.value_text import spell_number, spell_percent
from . import catalogue, view


NBSP = chr(0xA0)
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]"})
_LIVE_SCHEMES = frozenset({"", "http", "https", "mailto"})
_LINK_PATTERNS = (
    "reference",
    "link",
    "image_link",
    "image_reference",
    "short_reference",
    "short_image_ref",
    "autolink",
    "automail",
)


def create_app(connections: list[ConnectionConfig], *, static: bool = False) -> Flask:
    """Build the docs Flask app over `connections` - the CLI's already-resolved set.

    `static` renders for a built site, which states no time relative to the moment it was built.
    """

    app = Flask(__name__)
    app.jinja_env.undefined = ChainableUndefined  # absent keys render blank
    cast(dict[str, Any], app.jinja_env.globals)["static_site"] = static
    app.config["DOCS_CONNECTIONS"] = list(connections)

    _register_filters(app)
    _register_routes(app)
    app.before_request(_refuse_a_foreign_host)

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
    """Nav tree for the sidebar."""

    return {"nav": catalogue.nav_tree(connections)}


def _register_filters(app: Flask) -> None:
    app.add_template_filter(_render_markdown, "md")
    app.add_template_filter(_render_context, "context_md")
    app.add_template_filter(_non_breaking, "nbsp")
    app.add_template_filter(_number, "number")
    app.add_template_filter(_percent, "percent")
    app.add_template_filter(_pretty_datetime, "dt")
    app.add_template_filter(_relative_time, "relative")


def _render_markdown(text: str | None, mentions: Mapping[str, str] | None = None) -> Markup | str:
    """Render user Markdown as HTML: raw HTML shown as text, only web, mail and relative links live.

    A mention of a key of `mentions` in the rendered prose links to its href.
    """

    return Markup(_renderer(links=True, mentions=mentions).convert(text)) if text else ""


def _render_context(text: str) -> Markup:
    """Render a context fragment as HTML, its line breaks kept, raw HTML and links shown as text.

    The fragment quotes database values: a stored `<script>` or `[x](javascript:...)` stays text.
    """

    return Markup(_renderer(links=False, extensions=("nl2br",)).convert(text))


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


def _refuse_a_foreign_host() -> None:
    """400 for any `Host` but a loopback name; Flask's `TRUSTED_HOSTS` cannot admit `[::1]`."""

    if re.sub(r":\d+$", "", request.host).lower() not in LOOPBACK_HOSTS:
        abort(400)


def _renderer(
    *,
    links: bool,
    extensions: tuple[str, ...] = (),
    mentions: Mapping[str, str] | None = None,
) -> markdown.Markdown:
    renderer = markdown.Markdown(extensions=["tables", "fenced_code", *extensions])
    renderer.preprocessors.deregister("html_block")
    renderer.inlinePatterns.deregister("html")

    if mentions:
        renderer.treeprocessors.register(_LinkMentions(renderer, mentions), "mentions", -5)

    if links:
        renderer.treeprocessors.register(_DropRefusedTargets(renderer), "refuse_targets", -10)
    else:
        for pattern in _LINK_PATTERNS:
            renderer.inlinePatterns.deregister(pattern)

    return renderer


class _LinkMentions(Treeprocessor):
    def __init__(self, md: markdown.Markdown, mentions: Mapping[str, str]) -> None:
        super().__init__(md)
        self._mentions = mentions

    def run(self, root: Element) -> None:
        self._link(root)

    def _link(self, element: Element) -> None:
        element.text, children = self._split(element.text)

        for child in list(element):
            href = self._mentions.get(child.text or "") if child.tag == "code" else None

            if href is not None and len(child) == 0:
                anchor = Element("a", {"href": href})
                anchor.append(child)
                anchor.tail, child.tail = child.tail, None
                child = anchor
            elif child.tag not in ("a", "pre", "code"):
                self._link(child)

            child.tail, after = self._split(child.tail)
            children += [child, *after]

        element[:] = children

    def _split(self, text: str | None) -> tuple[str | None, list[Element]]:
        if not text:
            return text, []

        leading = ""
        anchors: list[Element] = []

        for run, href in view.mention_segments(text, self._mentions):
            if href is not None:
                anchors.append(Element("a", {"href": href}))
                anchors[-1].text = run
            elif anchors:
                anchors[-1].tail = (anchors[-1].tail or "") + run
            else:
                leading += run

        return leading, anchors


class _DropRefusedTargets(Treeprocessor):
    def run(self, root: Element) -> None:
        for element in root.iter():
            for attribute in ("href", "src"):
                target = element.get(attribute)

                if target is not None and not _live_target(target):
                    del element.attrib[attribute]


def _live_target(target: str) -> bool:
    # Judged as the browser reads it: character references decoded (an automail link arrives
    # with its `&` stashed), then split the way a browser strips control characters.
    decoded = html.unescape(target.replace(AMP_SUBSTITUTE, "&"))

    try:
        return urlsplit(decoded).scheme in _LIVE_SCHEMES
    except ValueError:
        return False
