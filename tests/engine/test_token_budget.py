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


def test_stop_at_first_overflow() -> None:
    # a=25 tokens, b=50 tokens, c=25 tokens; budget 30
    secs = [
        make_section("a", "x" * 100),
        make_section("b", "y" * 200),
        make_section("c", "z" * 100),
    ]
    out = select(secs, budget=30)
    assert [s.name for s in out.included] == ["a"]
    assert [s.name for s in out.omitted] == ["b", "c"]
    assert out.truncated is True
    assert out.used_tokens == 25


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
