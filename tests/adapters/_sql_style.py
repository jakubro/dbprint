"""The house SQL style (GUIDELINES.md 2, `adapters`), checked on one statement by token and parse tree.

An opaque command (`SHOW`, `DESCRIBE`) is held to casing alone; layout binds queries and DML only.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.dialects.clickhouse import ClickHouse
from sqlglot.parser import Parser
from sqlglot.tokens import Token, TokenType


_WORDLESS_TOKENS = frozenset(
    {
        TokenType.VAR,
        TokenType.IDENTIFIER,
        TokenType.STRING,
        TokenType.NATIONAL_STRING,
        TokenType.RAW_STRING,
        TokenType.BIT_STRING,
        TokenType.HEX_STRING,
        TokenType.BYTE_STRING,
        TokenType.NUMBER,
        TokenType.PARAMETER,
        TokenType.PLACEHOLDER,
    },
)


_CLICKHOUSE_FUNCTIONS = frozenset(ClickHouse.Parser.FUNCTIONS) | frozenset(
    ClickHouse.Parser.FUNCTION_PARSERS,
)


def violations(sql: str, dialect: str) -> list[str]:
    """Every departure from the house style in one statement, each naming the offending text."""

    tokens = sqlglot.tokenize(sql, dialect=dialect)
    found = _join_spellings(tokens)

    try:
        tree = sqlglot.parse_one(sql, dialect=dialect)
    except sqlglot.ParseError:
        tree = None

    if (
        tree is None
        or isinstance(tree, exp.Command)
        or not isinstance(tree, exp.Query | exp.DDL | exp.DML)
    ):
        return _casing(sql, tokens, set(), dialect) + found

    identifiers = {
        (node.meta["start"], node.meta["end"])
        for node in tree.find_all(exp.Identifier)
        if "start" in node.meta
    }

    return (
        _casing(sql, tokens, identifiers, dialect)
        + found
        + list(_alias_violations(tree, tokens))
        + list(_column_violations(tree, dialect))
        + list(_counting_violations(tree))
    )


def file_violations(text: str, dialect: str, *, casing_only: bool = False) -> list[str]:
    """`violations` over every statement of a hand-written file; casing alone when asked."""

    tokens = sqlglot.tokenize(text, dialect=dialect)
    trees = [] if casing_only else [t for t in sqlglot.parse(text, dialect=dialect) if t]
    identifiers = {
        (node.meta["start"], node.meta["end"])
        for tree in trees
        for node in tree.find_all(exp.Identifier)
        if "start" in node.meta
    }
    found = _casing(text, tokens, identifiers, dialect) + _join_spellings(tokens)

    # DDL keeps the casing rule alone; a query or an INSERT ... SELECT gets every rule.
    for tree in trees:
        if isinstance(tree, exp.Query | exp.DML):
            found += list(_alias_violations(tree, tokens))
            found += list(_column_violations(tree, dialect)) + list(_counting_violations(tree))

    return found


def alias_violations(sql: str, dialect: str) -> list[str]:
    """Every table or derived-table alias in one emitted query that is not three letters."""

    try:
        tree = sqlglot.parse_one(sql, dialect=dialect)
    except sqlglot.ParseError:
        return []

    return list(_short_aliases(tree))


def layout_violations(sql: str, dialect: str, *, template: str | None = None) -> list[str]:
    """Every departure from the house layout in one query, each naming its line or alias.

    Line length is read from `template`, the statement before its parameters were bound, when given.
    """

    try:
        tree = sqlglot.parse_one(sql, dialect=dialect)
    except sqlglot.ParseError:
        return []

    if isinstance(tree, exp.Command) or not isinstance(tree, exp.Query | exp.DML):
        return []

    return _layout(template or sql, sqlglot.tokenize(sql, dialect=dialect), tree)


def file_layout_violations(text: str, dialect: str) -> list[str]:
    """`layout_violations` over every query and DML statement of a hand-written file."""

    tokens = sqlglot.tokenize(text, dialect=dialect)
    statements: list[list[Token]] = [[]]

    for token in tokens:
        if token.token_type is TokenType.SEMICOLON:
            statements.append([])
        else:
            statements[-1].append(token)

    found = []

    for group in filter(None, statements):
        source = text[group[0].start : group[-1].end + 1]

        try:
            tree = sqlglot.parse_one(source, dialect=dialect)
        except sqlglot.ParseError:
            continue

        if isinstance(tree, exp.Query | exp.DML) and not isinstance(tree, exp.Command):
            found += _layout(source, group, tree, first_line=group[0].line)

    return found


def _casing(
    sql: str,
    tokens: list[Token],
    identifiers: set[tuple[int, int]],
    dialect: str,
) -> list[str]:
    out = []

    for index, token in enumerate(tokens):
        after_dot = index > 0 and tokens[index - 1].token_type is TokenType.DOT
        # Read from the source: a multi-word keyword (`CHARACTER VARYING`) arrives normalized.
        written = sql[token.start : token.end + 1]
        words = written.split()

        # A word after a dot is a name part (`c.table`, a `SET` key), never a keyword.
        if (token.start, token.end) in identifiers or after_dot or not words:
            continue

        if not all(word.isidentifier() for word in words):
            continue

        followed_by_paren = index + 1 < len(tokens) and tokens[index + 1].token_type is (
            TokenType.L_PAREN
        )

        if token.token_type in _WORDLESS_TOKENS:
            # A bare word before a parenthesis is a function name; ClickHouse spells its own.
            checked = followed_by_paren and dialect != "clickhouse"
        elif dialect == "clickhouse":
            # Its functions include keyword spellings (`any(`), which keep the ClickHouse case too.
            function = followed_by_paren and token.text.upper() in _CLICKHOUSE_FUNCTIONS
            checked = not (function or token.token_type in Parser.TYPE_TOKENS)
        else:
            checked = True

        if checked and written != written.upper():
            out.append(f"line {token.line}: lowercase {written!r}")

    return out


def _join_spellings(tokens: list[Token]) -> list[str]:
    kinds = [token.token_type for token in tokens]
    out = []

    for index in range(len(kinds) - 1):
        if kinds[index] is TokenType.INNER and kinds[index + 1] is TokenType.JOIN:
            out.append("INNER JOIN, not JOIN")

        if kinds[index : index + 3] == [TokenType.FULL, TokenType.OUTER, TokenType.JOIN]:
            out.append("FULL OUTER JOIN, not FULL JOIN")

    return out


def _alias_violations(tree: exp.Expression, tokens: list[Token]) -> Iterator[str]:
    ctes = {cte.alias_or_name for cte in tree.find_all(exp.CTE)}
    alias_starts = {token.start: index for index, token in enumerate(tokens)}

    for node in tree.find_all(exp.Table, exp.Subquery):
        if isinstance(node, exp.Table) and (
            node.name in ctes
            or _under_select_star(node)
            or isinstance(node.parent, exp.Create | exp.Drop | exp.Schema | exp.Insert)
        ):
            continue

        if isinstance(node, exp.Subquery) and not isinstance(node.parent, exp.From | exp.Join):
            continue

        alias = node.args.get("alias")

        if alias is None:
            yield f"unaliased {node.sql()[:60]!r}"
            continue

        start = alias.this.meta.get("start") if isinstance(alias.this, exp.Identifier) else None
        index = alias_starts.get(start) if start is not None else None

        if index and tokens[index - 1].token_type is TokenType.ALIAS:
            yield f"AS before the alias {alias.name!r}"


def _under_select_star(table: exp.Table) -> bool:
    """A `scope.filter` wrapper keeps its table unaliased so a filter may name it; so does a copy."""

    select = table.find_ancestor(exp.Select)

    return select is not None and all(isinstance(e, exp.Star) for e in select.expressions)


def _column_violations(tree: exp.Expression, dialect: str) -> Iterator[str]:
    ctes = {cte.alias_or_name for cte in tree.find_all(exp.CTE)}

    for column in tree.find_all(exp.Column):
        if (
            column.table
            or isinstance(column.this, exp.Star)
            or _names_output(column, ctes, dialect)
        ):
            continue

        yield f"unqualified column {column.name!r}"


def _names_output(column: exp.Column, ctes: set[str], dialect: str) -> bool:
    if column.find_ancestor(exp.Lambda) is not None:
        return True

    select = column.find_ancestor(exp.Select)

    if select is None:
        return True

    aliases = {e.alias for e in select.expressions if isinstance(e, exp.Alias)}

    if column.name in aliases and column.find_ancestor(exp.Order, exp.Group, exp.Having):
        return True

    # ClickHouse names an expression anywhere in the statement and reads it back by that name.
    if dialect == "clickhouse" and column.name in {a.alias for a in select.find_all(exp.Alias)}:
        return True

    sources = [t.name for t in select.find_all(exp.Table) if t.find_ancestor(exp.Select) is select]

    return bool(sources) and all(name in ctes for name in sources)


def _counting_violations(tree: exp.Expression) -> Iterator[str]:
    for count in tree.find_all(exp.Count):
        if isinstance(count.this, exp.Star):
            yield "COUNT(*), not COUNT(1)"

    for total in tree.find_all(exp.Sum):
        case = total.this

        if isinstance(case, exp.Case) and any(
            _tests_non_null(branch.this) for branch in case.args.get("ifs") or []
        ):
            yield "SUM(CASE WHEN ... IS NOT NULL ...), not COUNT(col)"


def _tests_non_null(condition: exp.Expression) -> bool:
    if isinstance(condition, exp.Not):
        return isinstance(condition.this, exp.Is)

    return isinstance(condition, exp.Is) and bool(condition.args.get("negate"))


# Each starts a line wherever it opens a clause of a query, never inside a call (`EXTRACT(... FROM`).
_CLAUSES = frozenset(
    {
        TokenType.SELECT,
        TokenType.FROM,
        TokenType.WHERE,
        TokenType.GROUP_BY,
        TokenType.HAVING,
        TokenType.QUALIFY,
        TokenType.ORDER_BY,
        TokenType.LIMIT,
    },
)

_JOIN_MODIFIERS = frozenset(
    {
        TokenType.LEFT,
        TokenType.RIGHT,
        TokenType.FULL,
        TokenType.CROSS,
        TokenType.OUTER,
        TokenType.INNER,
        TokenType.NATURAL,
        TokenType.SEMI,
        TokenType.ANTI,
        TokenType.ANY,
        TokenType.ARRAY,
    },
)

_MAX_LINE = 120
_ALIAS_LENGTH = 3
_ONE_LINER = 80


def _layout(
    text: str,
    tokens: list[Token],
    tree: exp.Expression,
    *,
    first_line: int = 1,
) -> list[str]:
    aliases = list(_short_aliases(tree))

    if _is_one_liner(text, tree):
        return aliases

    return _long_lines(text, first_line) + _clause_breaks(tokens) + _case_breaks(tokens) + aliases


def _short_aliases(tree: exp.Expression) -> Iterator[str]:
    for node in tree.find_all(exp.Table, exp.Subquery):
        alias = node.args.get("alias")

        if alias is not None and alias.name and len(alias.name) != _ALIAS_LENGTH:
            yield f"alias {alias.name!r}, not three letters"


def _is_one_liner(text: str, tree: exp.Expression) -> bool:
    sources = [*tree.find_all(exp.Table), *tree.find_all(exp.Subquery)]

    return (
        "\n" not in text.strip()
        and len(text.strip()) <= _ONE_LINER
        and len(sources) <= 1
        and tree.find(exp.Join, exp.Where) is None
    )


def _long_lines(text: str, first_line: int) -> list[str]:
    return [
        f"line {number}: {len(line)} characters, over {_MAX_LINE}"
        for number, line in enumerate(text.splitlines(), first_line)
        if len(line) > _MAX_LINE
    ]


def _clause_breaks(tokens: list[Token]) -> list[str]:
    out = []
    levels = [_Level(query=True, opened=-1)]

    for index, token in enumerate(tokens):
        kind = token.token_type
        level = levels[-1]

        if kind in (TokenType.L_PAREN, TokenType.L_BRACKET):
            following = tokens[index + 1].token_type if index + 1 < len(tokens) else None
            levels.append(
                _Level(query=following in (TokenType.SELECT, TokenType.WITH), opened=index),
            )
        elif kind in (TokenType.R_PAREN, TokenType.R_BRACKET):
            if len(levels) > 1:
                out += _closed_level(tokens, levels.pop(), index)
        elif not level.query:
            if kind is TokenType.COMMA:
                level.commas.append(index)
        elif kind in _CLAUSES:
            level.in_select_list = kind is TokenType.SELECT

            if kind is not TokenType.LIMIT:
                level.clauses.append(index)

            if not _starts_line(tokens, index):
                out.append(f"line {token.line}: {token.text} mid-line")
        elif kind is TokenType.JOIN:
            start = index

            while start > 0 and tokens[start - 1].token_type in _JOIN_MODIFIERS:
                start -= 1

            if not _starts_line(tokens, start):
                out.append(f"line {token.line}: a join mid-line")
        elif (
            kind is TokenType.COMMA and level.in_select_list and not _starts_line(tokens, index + 1)
        ):
            out.append(f"line {token.line}: two select-list items on one line")

    return out + _closed_level(tokens, levels[0], len(tokens))


@dataclass
class _Level:
    query: bool
    opened: int
    in_select_list: bool = False
    clauses: list[int] = field(default_factory=list)
    commas: list[int] = field(default_factory=list)


def _closed_level(tokens: list[Token], level: _Level, closed: int) -> list[str]:
    out = []

    if level.query:
        for index in level.clauses:
            body = index + 1

            if body < len(tokens) and tokens[body].token_type is TokenType.DISTINCT:
                body += 1

            if not _starts_line(tokens, body):
                out.append(
                    f"line {tokens[index].line}: {tokens[index].text} body on its keyword's line",
                )

    spans_lines = (
        0 <= level.opened
        and closed < len(tokens)
        and (tokens[closed].line > tokens[level.opened].line)
    )

    if not level.query and spans_lines and level.commas:
        edges = (level.opened + 1, closed)
        out += [
            f"line {tokens[index].line}: an argument on the opening or closing line of a call "
            "that spans lines"
            for index in edges
            if not _starts_line(tokens, index)
        ]
        out += [
            f"line {tokens[index].line}: two arguments on one line of a call that spans lines"
            for index in level.commas
            if not _starts_line(tokens, index + 1)
        ]

    return out


def _case_breaks(tokens: list[Token]) -> list[str]:
    out = []
    open_cases: list[list[int]] = []

    for index, token in enumerate(tokens):
        kind = token.token_type

        if kind is TokenType.CASE:
            open_cases.append([])
        elif kind in (TokenType.WHEN, TokenType.ELSE) and open_cases:
            open_cases[-1].append(index)
        elif kind is TokenType.END and open_cases:
            branches = open_cases.pop()

            if sum(tokens[i].token_type is TokenType.WHEN for i in branches) > 1:
                out += [
                    f"line {tokens[i].line}: {tokens[i].text} of a multi-branch CASE mid-line"
                    for i in [*branches, index]
                    if not _starts_line(tokens, i)
                ]

    return out


def _starts_line(tokens: list[Token], index: int) -> bool:
    return index <= 0 or index >= len(tokens) or tokens[index - 1].line < tokens[index].line
