"""SPEC 1.3's FQN syntax: joining separator-free segments and splitting the result gives them back."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from dbprint.spec.fqn import join, split


class TestJoinSplitProperties:
    @given(st.lists(st.text().filter(lambda part: "." not in part), min_size=1, max_size=5))
    def test_split_undoes_join(self, parts: list[str]) -> None:
        assert split(join(parts)) == tuple(parts)
