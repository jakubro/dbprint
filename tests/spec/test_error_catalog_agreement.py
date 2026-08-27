"""conformance/*.py's emitted Issue codes must agree with SPEC 6.3's catalog.

Compares SPEC 6.3's rows against the emitted codes in both directions, plus 6.4's totals.
"""

from __future__ import annotations

from tests.spec._issue_codes import emitted_codes
from tests.spec._spec_markdown import section, table_rows


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

    emitted = emitted_codes()

    assert len(emitted) >= 100
    assert "layout.missing-manifest" in emitted
    assert "schema.missing-required-field" in emitted


def test_every_emitted_code_is_catalogued() -> None:
    missing = sorted(set(emitted_codes()) - set(_catalog_codes()))

    assert not missing, f"emitted but not catalogued in SPEC 6.3: {missing}"


def test_every_catalogued_code_is_emitted() -> None:
    unused = sorted(set(_catalog_codes()) - set(emitted_codes()))

    assert not unused, f"catalogued in SPEC 6.3 but never emitted: {unused}"


def test_severities_agree() -> None:
    emitted = emitted_codes()
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
