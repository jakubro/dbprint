"""`paging.marked_line_pages`: every page within the bound, each but the last ending in a marker."""

from __future__ import annotations

import json

from hypothesis import given
from hypothesis import strategies as st

from dbprint.mcp import paging


_PIECES = ["a", "b", " ", "\n", "\r\n", '"', "\\", "\x01", "\u00e9", "\u4e2d"]


def _marker(n: int) -> str:
    return f"\n-- next_page: x?page={n}"


def _wire(text: str) -> int:
    return len(json.dumps(text, ensure_ascii=False)) - 2


class TestMarkedLinePages:
    @given(
        st.lists(st.sampled_from(_PIECES), max_size=2_000).map("".join),
        st.integers(min_value=40, max_value=400),
    )
    def test_pages_fit_carry_the_next_pages_marker_and_rejoin(self, text: str, bound: int) -> None:
        pages = paging.marked_line_pages(text, _marker, bound=bound, width=paging.escaped_width)
        markers = [_marker(n) for n in range(2, len(pages) + 1)]

        assert all(_wire(page) <= bound for page in pages)
        assert all(page.endswith(marker) for page, marker in zip(pages, markers, strict=False))
        assert not pages[-1].endswith(_marker(len(pages) + 1))
        assert (
            "".join(
                page.removesuffix(marker)
                for page, marker in zip(pages, [*markers, ""], strict=True)
            )
            == text
        )

    @given(st.lists(st.sampled_from(_PIECES), max_size=100).map("".join))
    def test_a_text_within_the_bound_is_one_page_with_no_marker(self, text: str) -> None:
        bound = _wire(text)

        assert paging.marked_line_pages(text, _marker, bound=bound, width=paging.escaped_width) == [
            text,
        ]
