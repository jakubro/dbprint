"""The naming rule for inferred foreign keys, a pure function over a catalog inventory: every
condition it imposes refuses an edge it cannot corroborate.
"""

from __future__ import annotations

import pytest

from dbprint.adapters.base import ColumnMeta, ForeignKeyMeta, TableType, UniqueKeyMeta
from dbprint.engine.inference import TableInventory, can_be_target, infer_foreign_keys


def _col(name: str, sql_type: str = "uuid") -> ColumnMeta:
    return ColumnMeta(name=name, sql_type=sql_type, nullable=True, default=None, ordinal=1)


def _table(
    fqn: str,
    columns: list[ColumnMeta],
    primary: str | None = None,
    unique: tuple[str, ...] = (),
    table_type: TableType = "table",
    keys_known: bool = True,
) -> TableInventory:
    return TableInventory(
        fqn=fqn,
        type=table_type,
        columns=tuple(columns),
        primary_key=primary,
        unique_columns=unique,
        keys_known=keys_known,
    )


def _from_keys(
    fqn: str,
    columns: list[ColumnMeta],
    keys: list[UniqueKeyMeta],
    table_type: TableType = "table",
) -> TableInventory:
    """Build an entry the way the orchestrator does, from declared-key groups."""

    return TableInventory.from_catalog(fqn, table_type, columns, keys)


def _inventory(*tables: TableInventory) -> dict[str, TableInventory]:
    return {t.fqn: t for t in tables}


def _table_edges(inventory: dict[str, TableInventory]) -> dict[str, list[tuple]]:
    """Every table's outgoing edges in a comparable shape: a same-named object that cannot
    be a target must not change what any other table infers.
    """

    return {
        fqn: [
            (edge.column, edge.target_table, edge.target_column)
            for edge in infer_foreign_keys(inv, inventory, [])
        ]
        for fqn, inv in inventory.items()
        if inv.type == "table"
    }


_USER = _table("public.curator", [_col("id"), _col("email", "text")], primary="id")


class TestTheClassicCase:
    def test_a_name_matching_pair_infers_an_edge(self) -> None:
        specimen_loan = _table(
            "public.specimen_loan",
            [_col("id"), _col("curator_id")],
            primary="id",
        )
        edges = infer_foreign_keys(specimen_loan, _inventory(specimen_loan, _USER), [])

        assert len(edges) == 1
        assert edges[0].column == ("curator_id",)
        assert edges[0].target_table == "public.curator"
        assert edges[0].target_column == ("id",)

    def test_a_self_reference_is_allowed(self) -> None:
        """`curator.curator_id` to `curator.id` satisfies the rule like any other pair."""

        curator = _table("public.curator", [_col("id"), _col("curator_id")], primary="id")
        edges = infer_foreign_keys(curator, _inventory(curator), [])

        assert [e.target_table for e in edges] == ["public.curator"]

    def test_a_column_never_references_itself(self) -> None:
        """A table keyed after its own name resolves the stem back to that key column."""

        curators = _from_keys(
            "public.curators",
            [_col("curator_id", "text")],
            [UniqueKeyMeta(columns=("curator_id",), primary=True)],
        )

        assert infer_foreign_keys(curators, _inventory(curators), []) == []

    def test_an_inferred_edge_carries_no_referential_action(self) -> None:
        """None was declared, so claiming one would put a fiction in the artifact."""

        specimen_loan = _table("public.specimen_loan", [_col("curator_id")])
        edge = infer_foreign_keys(specimen_loan, _inventory(specimen_loan, _USER), [])[0]

        assert (edge.on_delete, edge.on_update) == ("NO ACTION", "NO ACTION")
        assert edge.constraint_name is None


class TestTheRuleRefuses:
    def test_a_declared_key_is_never_duplicated(self) -> None:
        specimen_loan = _table("public.specimen_loan", [_col("curator_id")])
        declared = [
            ForeignKeyMeta(
                column=("curator_id",),
                target_table="public.curator",
                target_column=("id",),
                on_delete="CASCADE",
                on_update="NO ACTION",
                constraint_name="specimen_loan_curator_fk",
            ),
        ]

        assert infer_foreign_keys(specimen_loan, _inventory(specimen_loan, _USER), declared) == []

    def test_no_such_table_infers_nothing(self) -> None:
        specimen_loan = _table("public.specimen_loan", [_col("legacy_id")])

        assert infer_foreign_keys(specimen_loan, _inventory(specimen_loan, _USER), []) == []

    def test_a_target_without_a_declared_key_infers_nothing(self) -> None:
        """Uniqueness is the schema's statement, not a measurement of the data."""

        event = _table("public.event", [_col("id")])
        log = _table("public.log", [_col("event_id")])

        assert infer_foreign_keys(log, _inventory(log, event), []) == []

    def test_an_incompatible_type_infers_nothing(self) -> None:
        specimen_loan = _table("public.specimen_loan", [_col("curator_id", "text")])

        assert infer_foreign_keys(specimen_loan, _inventory(specimen_loan, _USER), []) == []

    def test_integer_widths_are_one_family(self) -> None:
        """A bigint key referenced by an integer column is drift, not counter-evidence."""

        curator = _table("public.curator", [_col("id", "bigint")], primary="id")
        entry = _table("public.entry", [_col("curator_id", "integer")])
        edges = infer_foreign_keys(entry, _inventory(entry, curator), [])

        assert [e.target_table for e in edges] == ["public.curator"]

    def test_a_column_without_the_suffix_infers_nothing(self) -> None:
        specimen_loan = _table("public.specimen_loan", [_col("curator")])

        assert infer_foreign_keys(specimen_loan, _inventory(specimen_loan, _USER), []) == []

    def test_a_bare_suffix_infers_nothing(self) -> None:
        """`_id` has an empty stem, so it names no table."""

        specimen_loan = _table("public.specimen_loan", [_col("_id")])

        assert infer_foreign_keys(specimen_loan, _inventory(specimen_loan, _USER), []) == []

    def test_an_ambiguous_stem_across_namespaces_infers_nothing(self) -> None:
        """An edge pointing at the wrong schema is worse than no edge."""

        left = _table("a.curator", [_col("id")], primary="id")
        right = _table("b.curator", [_col("id")], primary="id")
        specimen_loan = _table("c.specimen_loan", [_col("curator_id")])

        assert infer_foreign_keys(specimen_loan, _inventory(specimen_loan, left, right), []) == []

    def test_the_source_namespace_wins_over_a_foreign_one(self) -> None:
        own = _table("a.curator", [_col("id")], primary="id")
        other = _table("b.curator", [_col("id")], primary="id")
        specimen_loan = _table("a.specimen_loan", [_col("curator_id")])
        edges = infer_foreign_keys(specimen_loan, _inventory(specimen_loan, own, other), [])

        assert [e.target_table for e in edges] == ["a.curator"]


class TestCompositeKeys:
    def test_a_composite_target_key_is_not_a_target(self) -> None:
        """Naming evidence for a multi-column key is too weak to act on."""

        pair = _from_keys(
            "public.pair",
            [_col("a"), _col("b")],
            [UniqueKeyMeta(columns=("a", "b"), primary=True)],
        )
        child = _table("public.child", [_col("pair_id")])

        assert infer_foreign_keys(child, _inventory(child, pair), []) == []


class TestTheTargetColumn:
    """Which declared-unique column an edge points at: a normative order, not a preference."""

    @staticmethod
    def _target_column(target: TableInventory) -> tuple[str, ...] | None:
        """The column `specimen_loan.curator_id` resolves to on `target`, or None when it refuses."""

        source = _table("public.specimen_loan", [_col("curator_id", "text")])
        edges = infer_foreign_keys(source, _inventory(source, target), [])

        return edges[0].target_column if edges else None

    def test_a_namespaced_primary_key_beside_a_natural_unique(self) -> None:
        """`curators(curator_id PK, email UNIQUE)` - the commonest shape in the wild."""

        curators = _from_keys(
            "public.curators",
            [_col("curator_id", "text"), _col("email", "text")],
            [
                UniqueKeyMeta(columns=("curator_id",), primary=True),
                UniqueKeyMeta(columns=("email",)),
            ],
        )

        assert self._target_column(curators) == ("curator_id",)

    def test_the_classic_shape_still_targets_id(self) -> None:
        curators = _from_keys(
            "public.curators",
            [_col("id", "text"), _col("email", "text")],
            [
                UniqueKeyMeta(columns=("id",), primary=True),
                UniqueKeyMeta(columns=("email",)),
            ],
        )

        assert self._target_column(curators) == ("id",)

    def test_a_primary_key_beats_a_unique_column_called_id(self) -> None:
        curators = _from_keys(
            "public.curators",
            [_col("email", "text"), _col("id", "text")],
            [
                UniqueKeyMeta(columns=("email",), primary=True),
                UniqueKeyMeta(columns=("id",)),
            ],
        )

        assert self._target_column(curators) == ("email",)

    def test_a_sole_unique_constraint_is_the_target(self) -> None:
        curators = _from_keys(
            "public.curators",
            [_col("email", "text")],
            [UniqueKeyMeta(columns=("email",))],
        )

        assert self._target_column(curators) == ("email",)

    def test_two_uniques_and_no_primary_key_infer_nothing(self) -> None:
        """Several qualifying columns and no key to break the tie is an ambiguity."""

        curators = _from_keys(
            "public.curators",
            [_col("email", "text"), _col("postal_code", "text")],
            [
                UniqueKeyMeta(columns=("email",)),
                UniqueKeyMeta(columns=("postal_code",)),
            ],
        )

        assert self._target_column(curators) is None

    def test_one_column_declared_twice_is_not_an_ambiguity(self) -> None:
        curators = _from_keys(
            "public.curators",
            [_col("email", "text")],
            [
                UniqueKeyMeta(columns=("email",), primary=True),
                UniqueKeyMeta(columns=("email",)),
            ],
        )

        assert self._target_column(curators) == ("email",)

    def test_a_composite_primary_key_falls_through_to_the_sole_unique(self) -> None:
        """A composite key declares no single-column primary key, rather than none at all."""

        curators = _from_keys(
            "public.curators",
            [_col("cohort", "text"), _col("plot", "text"), _col("email", "text")],
            [
                UniqueKeyMeta(columns=("cohort", "plot"), primary=True),
                UniqueKeyMeta(columns=("email",)),
            ],
        )

        assert self._target_column(curators) == ("email",)

    def test_adding_a_unique_constraint_does_not_delete_an_edge(self) -> None:
        """A later unique constraint must not retarget or delete an edge already resolved."""

        columns = [_col("curator_id", "text"), _col("email", "text")]
        keys = [UniqueKeyMeta(columns=("curator_id",), primary=True)]
        before = self._target_column(_from_keys("public.curators", columns, keys))
        after = self._target_column(
            _from_keys("public.curators", columns, [*keys, UniqueKeyMeta(columns=("email",))]),
        )

        assert before == after == ("curator_id",)


class TestOnlyATableIsATarget:
    """A view originates edges like any other object but is never the target of one.

    One that could win a name would delete an edge between two unrelated tables.
    """

    def test_a_view_does_not_consume_an_exact_stem_match(self) -> None:
        """The stem tries `curator` before `curators`; a view answering to it is not an answer."""

        view = _table("fixture.curator", [_col("id")], table_type="view")
        curators = _table("fixture.curators", [_col("id")], primary="id")
        specimen_loan = _table("fixture.specimen_loan", [_col("curator_id")])
        edges = infer_foreign_keys(specimen_loan, _inventory(specimen_loan, view, curators), [])

        assert [e.target_table for e in edges] == ["fixture.curators"]

    def test_a_view_does_not_make_a_stem_ambiguous(self) -> None:
        """Two cross-namespace matches refuse; a view was never one of them."""

        curators = _table("b.curators", [_col("id")], primary="id")
        view = _table("c.curators", [_col("id")], table_type="view")
        specimen_loan = _table("fixture.specimen_loan", [_col("curator_id")])
        edges = infer_foreign_keys(specimen_loan, _inventory(specimen_loan, curators, view), [])

        assert [e.target_table for e in edges] == ["b.curators"]

    def test_a_view_alone_is_no_target(self) -> None:
        view = _table("fixture.curators", [_col("id")], primary="id", table_type="view")
        specimen_loan = _table("fixture.specimen_loan", [_col("curator_id")])

        assert infer_foreign_keys(specimen_loan, _inventory(specimen_loan, view), []) == []

    def test_a_matview_is_no_target_either(self) -> None:
        """Excluded on type alone (SPEC 2.3.8) - not whether the catalog reports it unique."""

        matview = _table("fixture.curators", [_col("id")], primary="id", table_type="matview")
        specimen_loan = _table("fixture.specimen_loan", [_col("curator_id")])

        assert infer_foreign_keys(specimen_loan, _inventory(specimen_loan, matview), []) == []

    def test_a_view_still_originates_an_edge(self) -> None:
        curators = _table("fixture.curators", [_col("id")], primary="id")
        view = _table("fixture.daily_specimen_loan", [_col("curator_id")], table_type="view")
        edges = infer_foreign_keys(view, _inventory(view, curators), [])

        assert [(e.column, e.target_table) for e in edges] == [
            (("curator_id",), "fixture.curators"),
        ]

    @pytest.mark.parametrize("shadow_type", ["view", "matview"])
    @pytest.mark.parametrize(
        ("shadow_fqn", "target_fqn"),
        [
            ("fixture.curator", "fixture.curators"),
            ("c.curators", "b.curators"),
        ],
    )
    def test_creating_one_changes_no_table_edges(
        self,
        shadow_fqn: str,
        target_fqn: str,
        shadow_type: TableType,
    ) -> None:
        """The invariant both shadowing routes reduce to, stated once."""

        curators = _table(target_fqn, [_col("id")], primary="id")
        specimen_loan = _table("fixture.specimen_loan", [_col("curator_id")])
        without = _inventory(specimen_loan, curators)
        shadow = _table(shadow_fqn, [_col("id")], primary="id", table_type=shadow_type)

        assert _table_edges(_inventory(specimen_loan, curators, shadow)) == _table_edges(without)


class TestAKeylessTableIsNoTargetEither:
    """A key-less table is invisible to resolution: SPEC 2.3.8 gates on eligibility, not type."""

    def test_a_keyless_table_does_not_consume_an_exact_stem_match(self) -> None:
        """The stem tries `curator` before `curators`; a key-less table is not an answer."""

        keyless = _table("fixture.curator", [_col("id")])
        curators = _table("fixture.curators", [_col("id")], primary="id")
        specimen_loan = _table("fixture.specimen_loan", [_col("curator_id")])
        edges = infer_foreign_keys(specimen_loan, _inventory(specimen_loan, keyless, curators), [])

        assert [e.target_table for e in edges] == ["fixture.curators"]

    def test_a_keyless_table_does_not_make_a_stem_ambiguous(self) -> None:
        """Two cross-namespace matches refuse; a key-less table was never one of them."""

        curators = _table("b.curators", [_col("id")], primary="id")
        keyless = _table("c.curators", [_col("id")])
        specimen_loan = _table("fixture.specimen_loan", [_col("curator_id")])
        edges = infer_foreign_keys(specimen_loan, _inventory(specimen_loan, curators, keyless), [])

        assert [e.target_table for e in edges] == ["b.curators"]

    def test_a_keyless_table_alone_is_no_target(self) -> None:
        keyless = _table("fixture.curators", [_col("id")])
        specimen_loan = _table("fixture.specimen_loan", [_col("curator_id")])

        assert infer_foreign_keys(specimen_loan, _inventory(specimen_loan, keyless), []) == []

    def test_a_composite_only_primary_key_is_ineligible(self) -> None:
        """A composite key is not the single-column rule's target, so the table is invisible."""

        composite = _from_keys(
            "fixture.curators",
            [_col("a"), _col("b")],
            [UniqueKeyMeta(columns=("a", "b"), primary=True)],
        )
        keyed_elsewhere = _table("c.curators", [_col("id")], primary="id")
        specimen_loan = _table("fixture.specimen_loan", [_col("curator_id")])
        edges = infer_foreign_keys(
            specimen_loan,
            _inventory(specimen_loan, composite, keyed_elsewhere),
            [],
        )

        assert [e.target_table for e in edges] == ["c.curators"]

    def test_several_uniques_no_primary_key_is_ineligible(self) -> None:
        """Two qualifying columns and no primary key makes the table invisible, not a dead end."""

        two_uniques = _table(
            "fixture.curators",
            [_col("id"), _col("email", "text")],
            unique=("id", "email"),
        )
        keyed_elsewhere = _table("c.curators", [_col("id")], primary="id")
        specimen_loan = _table("fixture.specimen_loan", [_col("curator_id")])
        edges = infer_foreign_keys(
            specimen_loan,
            _inventory(specimen_loan, two_uniques, keyed_elsewhere),
            [],
        )

        assert [e.target_table for e in edges] == ["c.curators"]

    @pytest.mark.parametrize(
        ("shadow_fqn", "target_fqn"),
        [
            ("fixture.curator", "fixture.curators"),
            ("c.curators", "b.curators"),
        ],
    )
    def test_creating_a_keyless_table_changes_no_other_tables_edges(
        self,
        shadow_fqn: str,
        target_fqn: str,
    ) -> None:
        """Two inventories differing only by a key-less same-named table produce identical
        edges for every table, over both the exact-stem and the ambiguity route.
        """

        curators = _table(target_fqn, [_col("id")], primary="id")
        specimen_loan = _table("fixture.specimen_loan", [_col("curator_id")])
        without = _inventory(specimen_loan, curators)
        keyless = _table(shadow_fqn, [_col("id")])

        # `_table_edges` reports the key-less table's own (empty) edges too, since it is
        # `type == "table"`, so that entry is excluded before comparing.
        with_shadow = _table_edges(_inventory(specimen_loan, curators, keyless))
        del with_shadow[shadow_fqn]

        assert with_shadow == _table_edges(without)

    def test_a_failed_key_read_suppresses_without_redirecting(self) -> None:
        """`keys_known=False` is a catalog read that raised, carried distinctly from a schema
        that declares nothing: it may take an edge away, never hand it to another table.
        """

        unknown = _table("fixture.curator", [_col("id")], keys_known=False)
        curators = _table("fixture.curators", [_col("id")], primary="id")
        specimen_loan = _table("fixture.specimen_loan", [_col("curator_id")])

        # It wins the exact-stem match and supplies no column, so the edge is suppressed.
        assert (
            infer_foreign_keys(specimen_loan, _inventory(specimen_loan, unknown, curators), [])
            == []
        )
        # And alone, it behaves like an ordinary key-less table.
        assert infer_foreign_keys(specimen_loan, _inventory(specimen_loan, unknown), []) == []

    def test_a_failed_key_read_still_counts_toward_ambiguity(self) -> None:
        """Unknown keys keep the object visible in the cross-namespace pass, so it can still
        turn an otherwise-unique stem into a refused ambiguity.
        """

        unknown = _table("c.curators", [_col("id")], keys_known=False)
        keyed = _table("b.curators", [_col("id")], primary="id")
        specimen_loan = _table("fixture.specimen_loan", [_col("curator_id")])

        assert (
            infer_foreign_keys(specimen_loan, _inventory(specimen_loan, unknown, keyed), []) == []
        )


class TestCanBeTarget:
    """The public eligibility predicate `relationships.yaml`'s `eligible_target` reports."""

    def test_a_keyed_table_is_eligible(self) -> None:
        assert can_be_target(_table("public.curators", [_col("id")], primary="id")) is True

    def test_a_sole_unique_constraint_is_eligible(self) -> None:
        curators = _from_keys(
            "public.curators",
            [_col("email", "text")],
            [UniqueKeyMeta(columns=("email",))],
        )

        assert can_be_target(curators) is True

    def test_a_keyless_table_is_ineligible(self) -> None:
        assert can_be_target(_table("public.curators", [_col("id")])) is False

    def test_a_composite_only_key_is_ineligible(self) -> None:
        composite = _from_keys(
            "fixture.curators",
            [_col("a"), _col("b")],
            [UniqueKeyMeta(columns=("a", "b"), primary=True)],
        )

        assert can_be_target(composite) is False

    @pytest.mark.parametrize("table_type", ["view", "matview"])
    def test_a_view_or_matview_is_ineligible_regardless_of_its_own_keys(
        self,
        table_type: TableType,
    ) -> None:
        keyed = _table("fixture.curators", [_col("id")], primary="id", table_type=table_type)

        assert can_be_target(keyed) is False

    def test_a_failed_keys_read_reports_eligible(self) -> None:
        """`keys_known=False` can only suppress an edge, never redirect one."""

        unknown = _table("fixture.curator", [_col("id")], keys_known=False)

        assert can_be_target(unknown) is True


class TestPluralTargets:
    """The dominant convention names tables plurally and columns singularly."""

    def test_a_singular_stem_reaches_a_plural_table(self) -> None:
        curators = _table("public.curators", [_col("id")], primary="id")
        specimen_loan = _table("public.specimen_loan", [_col("curator_id")])
        edges = infer_foreign_keys(specimen_loan, _inventory(specimen_loan, curators), [])

        assert [e.target_table for e in edges] == ["public.curators"]

    def test_an_exact_match_wins_over_a_plural_one(self) -> None:
        """A schema holding both resolves to the table the column actually names."""

        curator = _table("public.curator", [_col("id")], primary="id")
        curators = _table("public.curators", [_col("id")], primary="id")
        record = _table("public.record", [_col("curator_id")])
        edges = infer_foreign_keys(record, _inventory(record, curator, curators), [])

        assert [e.target_table for e in edges] == ["public.curator"]

    @pytest.mark.parametrize(
        ("stem", "table"),
        [
            ("category", "categories"),
            ("address", "addresses"),
            ("box", "boxes"),
            ("batch", "batches"),
            ("day", "days"),
        ],
    )
    def test_regular_plural_forms(self, stem: str, table: str) -> None:
        target = _table(f"public.{table}", [_col("id")], primary="id")
        source = _table("public.sample", [_col(f"{stem}_id")])
        edges = infer_foreign_keys(source, _inventory(source, target), [])

        assert [e.target_table for e in edges] == [f"public.{table}"]

    def test_an_irregular_plural_is_not_guessed(self) -> None:
        """No dictionary and no stemming: `genus_id` does not reach `genera`."""

        genera = _table("public.genera", [_col("id")], primary="id")
        record = _table("public.record", [_col("genus_id")])

        assert infer_foreign_keys(record, _inventory(record, genera), []) == []
