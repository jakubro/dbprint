"""Every conformance code the validator can emit, extracted from `conformance/*.py` itself.

Shared by the SPEC 6.3 catalog guard and the conformance index guard, so neither can drift.
"""

from __future__ import annotations

import ast
import types
from pathlib import Path

from dbprint.conformance.schema_validation import _FORWARD_COMPAT_ENUM_FIELDS, _classify


CONFORMANCE_DIR = Path(__file__).resolve().parents[2] / "src/dbprint/conformance"

# SPEC 6.3's own single-letter severity column, keyed by the Issue.severity string it means.
SEVERITY_MAP = {"error": "E", "warning": "W"}

# `_classify()` maps a jsonschema error to a code through variables, not literal `Issue(...)`
# arguments - SPEC 6.3's one documented indirection.
_INDIRECTION_FILE = "schema_validation.py"


def emitted_codes() -> dict[str, str]:
    """Every `code: severity` pair from an `Issue(...)` call site under conformance/.

    Reads `code`/`severity` by position (SPEC 6.2's dataclass order). A non-literal call is a
    guard failure outside `_INDIRECTION_FILE`, whose codes come from `_schema_validation_codes()`.
    """

    codes: dict[str, str] = {}

    for path in sorted(CONFORMANCE_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))

        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
                continue

            if node.func.id != "Issue":
                continue

            code_node = _positional_or_keyword(node, 1, "code")
            severity_node = _positional_or_keyword(node, 2, "severity")
            code_is_literal = isinstance(code_node, ast.Constant) and isinstance(
                code_node.value,
                str,
            )
            severity_is_literal = isinstance(severity_node, ast.Constant) and isinstance(
                severity_node.value,
                str,
            )

            if code_is_literal and severity_is_literal:
                codes[code_node.value] = SEVERITY_MAP[severity_node.value]
            elif path.name != _INDIRECTION_FILE:
                raise AssertionError(
                    f"{path}:{node.lineno}: Issue() call with a non-literal code or "
                    "severity - extend this extractor before trusting it",
                )

    codes.update(_schema_validation_codes())

    return codes


def _positional_or_keyword(call: ast.Call, index: int, name: str) -> ast.expr | None:
    """Return the argument at `index`, or the keyword named `name` when it was passed that way."""

    if len(call.args) > index:
        return call.args[index]

    return next((kw.value for kw in call.keywords if kw.arg == name), None)


def _schema_validation_codes() -> dict[str, str]:
    """Every `code: severity` pair `_classify()` can return, driven through every branch.

    Calling the real function cannot drift from what `_check()` does with its result.
    """

    def classify(validator: str, absolute_path: list[str]) -> tuple[str, str]:
        err = types.SimpleNamespace(validator=validator, absolute_path=absolute_path)
        code, severity, _ref = _classify(err, "§0")

        return code, SEVERITY_MAP[severity]

    branches: list[tuple[str, list[str]]] = [
        *(("enum", [field]) for field in _FORWARD_COMPAT_ENUM_FIELDS),
        ("enum", ["some_other_field"]),
        ("enum", []),
        ("required", []),
        ("pattern", ["percentiles"]),
        ("pattern", ["something_else"]),
        ("const", ["format_version"]),
        ("const", ["something_else"]),
        ("type", []),
    ]

    return dict(classify(validator, path) for validator, path in branches)
