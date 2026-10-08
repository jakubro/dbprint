"""Credential masks for the DDL of a table whose rows live on another server (SPEC 2.1.3).

Each replaces only the secret with `MASK`, the text ClickHouse itself writes for a hidden one.
"""

from __future__ import annotations

import re


MASK = "[HIDDEN]"

_URL_PASSWORD_RE = re.compile(
    r"\b[A-Za-z][A-Za-z0-9+.-]*://[^\s:/@]+:(?P<secret>\S+)@(?=[^\s@]*(?:\s|$))",
)
_ATTRIBUTE_PASSWORD_RE = re.compile(
    r"\b(?:PWD|Password)\s*=\s*(?P<secret>\{[^}]*\}|[^;,\s]+)",
    re.IGNORECASE,
)
_SPIDER_PASSWORD_RE = re.compile(
    r"\bpassword\s+(?P<quote>[\"'])(?P<secret>(?:(?!(?P=quote)).)*)(?P=quote)",
    re.IGNORECASE,
)
_OPTION_LITERAL_RE = re.compile(
    r"(?P<key>`?\b(?P<name>CONNECTION|OPTION_LIST|COMMENT|REMOTE_PASSWORD)`?\s*=\s*)"
    r"'(?P<value>(?:[^'\\]|\\.|'')*)'",
    re.IGNORECASE,
)
_MYSQL_ESCAPE_RE = re.compile(r"''|\\.")
_CLICKHOUSE_STRING_RE = re.compile(r"'(?P<value>(?:[^'\\]|\\.)*)'")
_CLICKHOUSE_ESCAPE_RE = re.compile(r"\\.")
_REMOTE_ENGINE_RE = re.compile(r"\bENGINE\s*=\s*(?:FEDERATED|CONNECT|SPIDER)\b", re.IGNORECASE)


def mask_secrets(text: str) -> str:
    """`text` with a URL's password, a `PWD=`/`Password=` value and a Spider `password` masked."""

    patterns = (_URL_PASSWORD_RE, _ATTRIBUTE_PASSWORD_RE, _SPIDER_PASSWORD_RE)

    return _spliced(text, _spans(text, patterns))


def mask_mysql_ddl(ddl: str) -> str:
    """A FEDERATED, CONNECT or SPIDER table's DDL with every option-form credential masked.

    Only `NAME='...'` table and partition options are read; a column's `COMMENT '...'` is not.
    """

    if not _REMOTE_ENGINE_RE.search(ddl):
        return ddl

    def masked(match: re.Match[str]) -> str:
        if match["name"].upper() == "REMOTE_PASSWORD":
            return f"{match['key']}'{MASK}'"

        return f"{match['key']}'{_masked_literal(match['value'], _MYSQL_ESCAPE_RE)}'"

    return _OPTION_LITERAL_RE.sub(masked, ddl)


def mask_clickhouse_strings(text: str) -> str:
    """Every `'...'` string literal in `text` with a URL's password masked inside it."""

    return _CLICKHOUSE_STRING_RE.sub(
        lambda m: f"'{_masked_literal(m['value'], _CLICKHOUSE_ESCAPE_RE, url_only=True)}'",
        text,
    )


def _masked_literal(raw: str, escape: re.Pattern[str], *, url_only: bool = False) -> str:
    # Mask the unescaped text, then splice over the raw span: an escaped quote stays in a password.
    starts: list[int] = []
    chars: list[str] = []
    index = 0

    while index < len(raw):
        match = escape.match(raw, index)
        width = len(match[0]) if match else 1
        starts.append(index)
        chars.append(raw[index + width - 1])
        index += width

    starts.append(len(raw))
    decoded = "".join(chars)
    patterns = (
        (_URL_PASSWORD_RE,)
        if url_only
        else (
            _URL_PASSWORD_RE,
            _ATTRIBUTE_PASSWORD_RE,
            _SPIDER_PASSWORD_RE,
        )
    )
    spans = [(starts[a], starts[b]) for a, b in _spans(decoded, patterns)]

    return _spliced(raw, spans)


def _spans(text: str, patterns: tuple[re.Pattern[str], ...]) -> list[tuple[int, int]]:
    found = {
        match.span("secret")
        for pattern in patterns
        for match in pattern.finditer(text)
        if match["secret"] != MASK
    }

    return sorted(found)


def _spliced(text: str, spans: list[tuple[int, int]]) -> str:
    out: list[str] = []
    cursor = 0

    for start, end in spans:
        if start < cursor:
            continue

        out.extend((text[cursor:start], MASK))
        cursor = end

    out.append(text[cursor:])

    return "".join(out)
