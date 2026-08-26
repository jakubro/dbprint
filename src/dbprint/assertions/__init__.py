"""Assertion DSL evaluator package per ASSERTIONS.md.

`parse_block` turns the raw YAML dict into a typed `AssertionSet`, raising `ParseError` only
when the block itself is not a mapping; the two `evaluate_*` entry points run the statistic
and SQL assertions.
"""

from __future__ import annotations

from .parser import (
    AssertionSet,
    ParseError,
    ParseFault,
    QueryAssertion,
    TablePredicates,
)
from .parser import (
    parse as parse_block,
)
from .sql import evaluate as evaluate_sql_assertions
from .statistic import evaluate as evaluate_statistic_assertions


__all__ = [
    "AssertionSet",
    "ParseError",
    "ParseFault",
    "QueryAssertion",
    "TablePredicates",
    "evaluate_sql_assertions",
    "evaluate_statistic_assertions",
    "parse_block",
]
