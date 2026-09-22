"""Token-budget allocator behavior."""

from __future__ import annotations

from dbprint.engine.token_budget import make_section, select, tokens_of, truncation_marker


def test_tokens_of_empty_is_zero() -> None:
    assert tokens_of("") == 0


def test_tokens_of_lower_bound_one() -> None:
    assert tokens_of("x") == 1


def test_tokens_of_long_text() -> None:
    text = "x" * 100
    assert tokens_of(text) == 25


def test_no_budget_includes_all() -> None:
    secs = [make_section("a", "x" * 100), make_section("b", "y" * 200)]
    out = select(secs, None)
    assert out.included == tuple(secs)
    assert out.omitted == ()
    assert out.truncated is False


def test_budget_fits_everything() -> None:
    secs = [make_section("a", "x" * 100), make_section("b", "y" * 100)]
    out = select(secs, budget=200)
    assert len(out.included) == 2
    assert out.truncated is False


def _abc() -> list:
    """a=25 tokens, b=50, c=25 - the shape every budget case below is measured against."""

    return [
        make_section("a", "x" * 100),
        make_section("b", "y" * 200),
        make_section("c", "z" * 100),
    ]


def test_a_section_that_does_not_fit_is_skipped_rather_than_blocking() -> None:
    out = select(_abc(), budget=60)

    assert [s.name for s in out.included] == ["a", "c"]
    assert [s.name for s in out.omitted] == ["b"]
    assert out.truncated is True
    assert out.used_tokens == 50


def test_a_later_section_still_has_to_fit() -> None:
    """Filling past an overflow is not ignoring the budget."""

    out = select(_abc(), budget=30)

    assert [s.name for s in out.included] == ["a"]
    assert [s.name for s in out.omitted] == ["b", "c"]
    assert out.used_tokens == 25


def test_a_pinned_section_is_charged_before_anything_else() -> None:
    """Offered first wherever it sits in the caller's own order."""

    sections = [*_abc(), make_section("header", "h" * 100, pinned=True)]
    out = select(sections, budget=30)

    assert [s.name for s in out.included] == ["header"]
    assert out.used_tokens == 25


def test_a_pinned_section_that_does_not_fit_is_still_dropped() -> None:
    """The flag decides the order it is offered in, never whether the budget applies."""

    out = select([make_section("header", "h" * 100, pinned=True)], budget=1)

    assert out.included == ()
    assert [s.name for s in out.omitted] == ["header"]
    assert out.used_tokens == 0


def test_included_keeps_the_callers_own_order() -> None:
    """`included` is the render order, so pinning must not reorder the output."""

    sections = [make_section("a", "x" * 100), make_section("header", "h" * 40, pinned=True)]
    out = select(sections, budget=100)

    assert [s.name for s in out.included] == ["a", "header"]


def test_truncation_marker_when_present() -> None:
    secs = [make_section("a", "x" * 100), make_section("b", "y" * 100)]
    out = select(secs, budget=20)
    marker = truncation_marker(out)
    assert marker.startswith("<!-- truncated:")
    assert "omitted: a, b" in marker


def test_no_marker_when_complete() -> None:
    secs = [make_section("a", "x" * 100)]
    out = select(secs, budget=1000)
    assert truncation_marker(out) == ""
