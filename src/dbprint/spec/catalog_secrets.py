"""The credential masks SPEC 2.1.3 applies to catalog text a print publishes.

A secret written into a view body, a default, a constraint or a comment is replaced by `MASK`;
the key, the quotes and a URL's user and host around it stay. Identifiers, types and operators
are never rewritten. Pure: no I/O, no state.
"""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass

from .secret_formats import secret_spans
from .sensitivity import names_credential


MASK = "[HIDDEN]"


def names_secret(key: str) -> bool:
    """True when `key` in `key=value` text names a secret (SPEC 2.1.3)."""

    return names_credential(key) or re.sub(r"[^a-z0-9]", "", key.lower()) in _SECRET_KEYS


def mask_text(text: str) -> str:
    """`text` read as one string literal's content, every secret in it replaced by `MASK`."""

    return _spliced(text, _text_spans(text))


def mask_ddl(text: str) -> str:
    """SQL `text` with every secret SPEC 2.1.3 names replaced by `MASK`, and nothing else changed.

    The dialect is unknown, so literals are read with a backslash as itself and as an escape, and
    `/*!...*/` as code and as a comment; whatever any reading finds is masked.
    """

    spans = [m.span("value") for m in _URL_PASSWORD_RE.finditer(text) if _maskable(m["value"])]
    spans += secret_spans(text)

    for backslash_escapes, executable_comments in itertools.product((False, True), repeat=2):
        spans += _literal_spans(
            text,
            backslash_escapes=backslash_escapes,
            executable_comments=executable_comments,
        )

    return _spliced(text, spans)


_SECRET_KEYS = frozenset(
    {"password", "passwd", "pwd", "pw", "secret", "token", "apikey", "accesskey", "privatekey"},
)
_NEVER_SECRET = frozenset({"", MASK.lower(), "null", "true", "false"})

_URL_PASSWORD_RE = re.compile(
    r"\b[A-Za-z][A-Za-z0-9+.-]*://[^\s:/@]*:(?P<value>\S+)@(?=[^\s@]*(?:\s|$))",
)
_KEY = r"(?<![A-Za-z0-9_.\-])(?P<kq>[\"']?)(?P<key>[A-Za-z0-9_.\-]+)(?P=kq)"
_PAIR_RE = re.compile(
    _KEY + r"\s*(?:=>|=|:(?![:/]))\s*"
    r"(?:\"(?P<dq>[^\"]*)\"|'(?P<sq>[^']*)'|(?P<odbc>\{[^}]*\})|(?P<bare>[^\s;,&'\"(){}\[\]<>]+))",
)
_PAIR_VALUES = ("dq", "sq", "odbc", "bare")
_QUOTED_VALUE_RE = re.compile(
    r"(?<![A-Za-z0-9_.\-])(?P<key>[A-Za-z0-9_.\-]+)\s+(?P<q>[\"'])(?P<value>(?:(?!(?P=q)).)*)(?P=q)",
)
_CAST_RE = re.compile(
    r"::\s*(?:character\s+varying|double\s+precision|bit\s+varying"
    r"|(?:timestamp|time)(?:\s+with(?:out)?\s+time\s+zone)?|\"?[A-Za-z_][\w.]*\"?)"
    r"(?:\(\s*\d+(?:\s*,\s*\d+)?\s*\))?(?:\[\])*$",
)
_TRAILING_KEY_RE = re.compile(r"[A-Za-z0-9_.$]+$")
# MySQL runs the body of `/*!NNNNN ... */`: SHOW CREATE TABLE wraps partitioning in one.
_EXECUTABLE_COMMENT_RE = re.compile(r"/\*M?!\d*")


@dataclass(frozen=True)
class _Quoted:
    quote: str
    open: int
    close: int
    content: str
    offsets: tuple[int, ...]

    def raw(self, start: int, end: int) -> tuple[int, int]:
        return self.offsets[start], self.offsets[end]


def _text_spans(text: str) -> list[tuple[int, int]]:
    spans = [m.span("value") for m in _URL_PASSWORD_RE.finditer(text) if _maskable(m["value"])]
    spans += secret_spans(text)
    spans += _pair_spans(text)
    spans += [
        m.span("value")
        for m in _QUOTED_VALUE_RE.finditer(text)
        if names_secret(m["key"]) and _maskable(m["value"])
    ]

    return spans


def _pair_spans(text: str) -> list[tuple[int, int]]:
    # A pair whose key is no secret may hold one in its value (`config="password=x"`): look inside.
    spans: list[tuple[int, int]] = []
    index = 0

    while match := _PAIR_RE.search(text, index):
        group = next(name for name in _PAIR_VALUES if match[name] is not None)

        if names_secret(match["key"]) and _maskable(match[group]):
            spans.append(match.span(group))
            index = match.end()
        else:
            index = match.start(group)

    return spans


def _literal_spans(
    text: str,
    *,
    backslash_escapes: bool,
    executable_comments: bool,
) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    previous: _Quoted | None = None
    code_start = 0

    runs = _quoted_runs(
        text,
        backslash_escapes=backslash_escapes,
        executable_comments=executable_comments,
    )

    for run in runs:
        if run.quote in "'\"":
            spans += [run.raw(start, end) for start, end in _text_spans(run.content)]

        key = _key_before(text[code_start : run.open], previous)

        if run.quote == "'" and key is not None and names_secret(key) and _maskable(run.content):
            spans.append(run.raw(0, len(run.content)))

        previous = run
        code_start = run.close

    return spans


def _quoted_runs(
    text: str,
    *,
    backslash_escapes: bool,
    executable_comments: bool,
) -> list[_Quoted]:
    runs: list[_Quoted] = []
    index = 0

    while index < len(text):
        if text.startswith("--", index):
            newline = text.find("\n", index)
            index = len(text) if newline == -1 else newline
        elif executable_comments and (opener := _EXECUTABLE_COMMENT_RE.match(text, index)):
            index = opener.end()
        elif text.startswith("/*", index):
            close = text.find("*/", index + 2)
            index = len(text) if close == -1 else close + 2
        elif text[index] in "'\"`":
            run = _quoted(text, index, backslash_escapes=backslash_escapes and text[index] != "`")
            runs.append(run)
            index = run.close
        else:
            index += 1

    return runs


def _quoted(text: str, start: int, *, backslash_escapes: bool) -> _Quoted:
    quote = text[start]
    chars: list[str] = []
    offsets: list[int] = []
    index = start + 1

    while index < len(text):
        if (
            backslash_escapes
            and text[index] == "\\"
            and index + 1 < len(text)
            or text.startswith(quote * 2, index)
        ):
            width = 2
        elif text[index] == quote:
            return _Quoted(quote, start, index + 1, "".join(chars), (*offsets, index))
        else:
            width = 1

        offsets.append(index)
        chars.append(text[index + width - 1])
        index += width

    return _Quoted(quote, start, len(text), "".join(chars), (*offsets, len(text)))


def _key_before(code: str, previous: _Quoted | None) -> str | None:
    # Through `)` and one cast: Postgres deparses a comparison as `((password)::text = 'v'::text)`.
    head = code.rstrip()

    if head.endswith("=>"):
        head = _through_one_cast(head[:-2])
    elif head.endswith("="):
        head = _through_one_cast(head[:-1])
    elif len(head) == len(code):
        return None

    if head:
        match = _TRAILING_KEY_RE.search(head)

        return match[0] if match else None

    return previous.content if previous is not None and previous.quote in '"`' else None


def _through_one_cast(head: str) -> str:
    head = head.rstrip().rstrip(")").rstrip()

    return _CAST_RE.sub("", head, count=1).rstrip().rstrip(")").rstrip()


def _maskable(value: str) -> bool:
    return value.strip().lower() not in _NEVER_SECRET


def _spliced(text: str, spans: list[tuple[int, int]]) -> str:
    merged: list[tuple[int, int]] = []

    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))

    out: list[str] = []
    cursor = 0

    for start, end in merged:
        out.extend((text[cursor:start], MASK))
        cursor = end

    out.append(text[cursor:])

    return "".join(out)
