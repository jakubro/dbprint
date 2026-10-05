"""Part paths per SPEC 2.2.18: one canonical spelling, a parent relation, and which parts are kept."""

from __future__ import annotations

import pytest

from dbprint.spec.classification import classify
from dbprint.spec.parts import (
    canonical,
    depth,
    display,
    is_canonical,
    last_member,
    member,
    parent,
    select_parts,
)


@pytest.mark.parametrize(
    ("name", "step"),
    [
        ("status", ".status"),
        ("_x9", "._x9"),
        ("user-id", '["user-id"]'),
        ("9lives", '["9lives"]'),
        ("a.b", '["a.b"]'),
        ("[x]", '["[x]"]'),
        ("é", '["é"]'),
        ('a"b\\', '["a\\"b\\\\"]'),
        ("\x01\n", '["\\u0001\\n"]'),
    ],
)
def test_a_member_takes_the_plain_form_only_for_a_plain_name(name: str, step: str) -> None:
    assert member(name) == step


@pytest.mark.parametrize(
    "path",
    ["[*]", "[keys]", ".status", "[*].sku", '["user-id"]', ".a[*][keys]", '["a.b"].c'],
)
def test_a_canonical_path_respells_as_itself(path: str) -> None:
    assert canonical(path) == path
    assert is_canonical(path)


@pytest.mark.parametrize(
    ("spelled", "expected"),
    [('["status"]', ".status"), ('["\\u0041"]', ".A"), ('["a\\/b"]', '["a/b"]')],
)
def test_a_needlessly_quoted_member_is_not_canonical(spelled: str, expected: str) -> None:
    assert canonical(spelled) == expected
    assert not is_canonical(spelled)


@pytest.mark.parametrize("path", ["", "status", ".", ".9a", "[0]", '["open', "[*]x", '.["status"]'])
def test_a_path_the_grammar_does_not_admit_is_refused(path: str) -> None:
    assert not is_canonical(path)


def test_a_part_sits_inside_its_longest_proper_prefix() -> None:
    assert parent("[*].sku") == "[*]"
    assert parent('.a["b c"][*]') == '.a["b c"]'
    assert parent(".status") is None
    assert depth('.a["b c"][*]') == 3
    assert last_member("[*].sku[*]") == "sku"
    assert last_member("[keys]") is None
    assert display("items", "[*].sku") == "items[*].sku"


def test_parts_are_kept_by_occurrences_then_path_text() -> None:
    candidates = {".b": 10, ".a": 10, ".c": 30, ".d": 1}

    assert select_parts(candidates, 3, 3) == (".c", ".a", ".b")


def test_a_child_outnumbering_its_parent_waits_for_it() -> None:
    candidates = {".tags": 10, ".tags[*]": 40, ".z": 20}

    assert select_parts(candidates, 3, 3) == (".z", ".tags", ".tags[*]")


def test_a_child_of_a_part_left_out_is_never_kept() -> None:
    candidates = {".a": 5, ".b": 9, ".a.x": 4}

    assert select_parts(candidates, 1, 3) == (".b",)
    assert select_parts({".a.x": 4}, 5, 3) == ()


def test_a_path_deeper_than_the_bound_is_never_kept() -> None:
    assert select_parts({".a": 3, ".a.b": 3, ".a.b.c": 3}, 10, 2) == (".a", ".a.b")


def test_a_descended_column_is_composite_unless_it_holds_json() -> None:
    assert classify("STRUCT(a INTEGER)[]", None, False, 50, has_parts=True) == "composite"
    assert classify("MAP(VARCHAR, VARCHAR)", None, False, 50, has_parts=True) == "composite"
    assert classify("jsonb", 40, False, 50, has_parts=True) == "json"
    assert classify("STRUCT(a INTEGER)[]", None, False, 50) == "unsupported"
