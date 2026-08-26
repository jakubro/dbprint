"""conformance/*.py's emitted Issue codes must agree with SPEC 6.3's catalog.

Extracts every `Issue(...)` call site's code and severity via AST - immune to the filename
and statistic-name literals that share a `<word>.<word>` shape - and every row from SPEC
6.3's markdown, then compares both directions plus 6.4's stated totals.
"""

from __future__ import annotations

import ast
import types
from pathlib import Path

from dbprint.conformance.schema_validation import _FORWARD_COMPAT_ENUM_FIELDS, _classify
from tests.spec._spec_markdown import section, table_rows


CONFORMANCE_DIR = Path(__file__).resolve().parents[2] / "src/dbprint/conformance"

# SPEC 6.3's own single-letter severity column, keyed by the Issue.severity string it means.
_SEVERITY_MAP = {"error": "E", "warning": "W"}

# `_classify()` maps a jsonschema error to a code through variables, not literal `Issue(...)`
# arguments - SPEC 6.3's one documented indirection.
_INDIRECTION_FILE = "schema_validation.py"


def _positional_or_keyword(call: ast.Call, index: int, name: str) -> ast.expr | None:
    if len(call.args) > index:
        return call.args[index]

    return next((kw.value for kw in call.keywords if kw.arg == name), None)


def _extract_issue_codes() -> dict[str, str]:
    """Every `code: severity` pair from an `Issue(...)` call site under conformance/.

    Reads `code`/`severity` by position (SPEC 6.2's dataclass order), which a filename or
    statistic-name literal used as `path` or `detail` can never occupy.
    `_INDIRECTION_FILE`'s non-literal calls go to `_schema_validation_codes()`; every other
    file's non-literal call is a guard failure.
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
                codes[code_node.value] = _SEVERITY_MAP[severity_node.value]
            elif path.name != _INDIRECTION_FILE:
                raise AssertionError(
                    f"{path}:{node.lineno}: Issue() call with a non-literal code or "
                    "severity - extend this extractor before trusting it",
                )

    codes.update(_schema_validation_codes())

    return codes


def _schema_validation_codes() -> dict[str, str]:
    """Every `code: severity` pair `_classify()` can return, driven through every branch.

    Calling the real function is a more faithful read than reparsing its return statements -
    it cannot silently drift from what `_check()` actually does with the result.
    """

    def classify(validator: str, absolute_path: list[str]) -> tuple[str, str]:
        err = types.SimpleNamespace(validator=validator, absolute_path=absolute_path)
        code, severity, _ref = _classify(err, "§0")

        return code, _SEVERITY_MAP[severity]

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


def _catalog_codes() -> dict[str, str]:
    """Every `code: severity` pair from SPEC 6.3's markdown, across all ten groups."""

    block = section("### 6.3 Error catalog", "### 6.4 Catalog totals")
    codes: dict[str, str] = {}

    for row in table_rows(block):
        if len(row) != 3 or not row[0].startswith("`"):
            continue  # the repeated `| Code | Sev | Trigger |` header, once per group

        codes[row[0].strip("`")] = row[1]

    return codes


def test_the_catalog_still_parses() -> None:
    """A guard that silently matched nothing would pass forever."""

    catalog = _catalog_codes()

    assert len(catalog) >= 100
    assert "layout.missing-manifest" in catalog


def test_the_extractor_still_finds_codes() -> None:
    """Same guard, other side - a broken AST walk would also pass forever."""

    emitted = _extract_issue_codes()

    assert len(emitted) >= 100
    assert "layout.missing-manifest" in emitted
    assert "schema.missing-required-field" in emitted


def test_every_emitted_code_is_catalogued() -> None:
    missing = sorted(set(_extract_issue_codes()) - set(_catalog_codes()))

    assert not missing, f"emitted but not catalogued in SPEC 6.3: {missing}"


def test_every_catalogued_code_is_emitted() -> None:
    unused = sorted(set(_catalog_codes()) - set(_extract_issue_codes()))

    assert not unused, f"catalogued in SPEC 6.3 but never emitted: {unused}"


def test_severities_agree() -> None:
    emitted = _extract_issue_codes()
    catalog = _catalog_codes()
    mismatched = sorted(
        code for code in set(emitted) & set(catalog) if emitted[code] != catalog[code]
    )

    assert not mismatched, f"SPEC 6.3 severity disagrees with the code for: {mismatched}"


def test_totals_agree_with_6_4() -> None:
    catalog = _catalog_codes()
    errors = sum(1 for sev in catalog.values() if sev == "E")
    warnings = sum(1 for sev in catalog.values() if sev == "W")
    totals_text = section("### 6.4 Catalog totals", "The catalog MAY grow")

    assert f"**{len(catalog)} codes**" in totals_text
    assert f"**{errors} error**" in totals_text
    assert f"**{warnings} warning**" in totals_text
