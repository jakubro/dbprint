"""Project config loader tests - discovery, defaults cascade, validation."""

from __future__ import annotations

from pathlib import Path
from typing import get_args

import pytest

from dbprint.config import (
    ConfigError,
    ConnectionConfig,
    DiffConfig,
    ProjectConfig,
    StatisticsConfig,
    load_project,
    load_project_at,
)
from dbprint.spec.looks_like import LooksLike
from dbprint.spec.sensitivity import Sensitivity


EXAMPLE_DIR = Path(__file__).parent.parent.parent / "docs/format/v1/examples/production"


def _write_config(tmp_path: Path, body: str) -> Path:
    cfg = tmp_path / ".dbprint.yaml"
    cfg.write_text(body)

    return tmp_path


class TestDiscovery:
    def test_loads_from_cwd(self, tmp_path: Path) -> None:
        _write_config(
            tmp_path,
            """
connections:
  primary:
    adapter: postgres
""",
        )
        cfg = load_project(tmp_path)
        assert cfg.project_root == tmp_path.resolve()
        assert "primary" in cfg.connections

    def test_walks_up_to_find_config(self, tmp_path: Path) -> None:
        _write_config(
            tmp_path,
            """
connections:
  primary:
    adapter: postgres
""",
        )
        deep = tmp_path / "a" / "b" / "c"
        deep.mkdir(parents=True)
        cfg = load_project(deep)
        assert cfg.project_root == tmp_path.resolve()

    def test_missing_config_raises_clear_error(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="no .dbprint.yaml found"):
            load_project(tmp_path)


class TestExactResolution:
    """`load_project_at` - the `--project` locator's own loader, no walk in either direction."""

    def test_loads_from_the_directory(self, tmp_path: Path) -> None:
        _write_config(
            tmp_path,
            """
connections:
  primary:
    adapter: postgres
""",
        )
        cfg = load_project_at(tmp_path)
        assert cfg.project_root == tmp_path.resolve()
        assert "primary" in cfg.connections

    def test_loads_from_the_config_file_itself(self, tmp_path: Path) -> None:
        _write_config(
            tmp_path,
            """
connections:
  primary:
    adapter: postgres
""",
        )
        cfg = load_project_at(tmp_path / ".dbprint.yaml")
        assert cfg.project_root == tmp_path.resolve()

    def test_never_walks_up_from_a_non_direct_child(self, tmp_path: Path) -> None:
        _write_config(
            tmp_path,
            """
connections:
  primary:
    adapter: postgres
""",
        )
        deep = tmp_path / "a" / "b" / "c"
        deep.mkdir(parents=True)

        with pytest.raises(ConfigError, match="no .dbprint.yaml at"):
            load_project_at(deep)

    def test_never_scans_downward(self, tmp_path: Path) -> None:
        nested = tmp_path / "nested"
        nested.mkdir()
        _write_config(
            nested,
            """
connections:
  primary:
    adapter: postgres
""",
        )

        with pytest.raises(ConfigError, match="no .dbprint.yaml at"):
            load_project_at(tmp_path)

    def test_missing_config_names_the_exact_path_checked(self, tmp_path: Path) -> None:
        absent = tmp_path / "absent"

        with pytest.raises(ConfigError, match=str(absent / ".dbprint.yaml")):
            load_project_at(absent)

    def test_accepts_a_relative_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write_config(
            tmp_path,
            """
connections:
  primary:
    adapter: postgres
""",
        )
        monkeypatch.chdir(tmp_path.parent)
        cfg = load_project_at(tmp_path.name)
        assert cfg.project_root == tmp_path.resolve()

    def test_accepts_a_string_locator(self, tmp_path: Path) -> None:
        """The CLI hands this a bare string, never a Path."""

        _write_config(
            tmp_path,
            """
connections:
  primary:
    adapter: postgres
""",
        )
        cfg = load_project_at(str(tmp_path))
        assert cfg.project_root == tmp_path.resolve()


class TestParsing:
    def test_minimal_connection_applies_defaults(self, tmp_path: Path) -> None:
        _write_config(
            tmp_path,
            """
connections:
  primary:
    adapter: postgres
""",
        )
        cfg = load_project(tmp_path)
        conn = cfg.connections["primary"]
        assert isinstance(conn, ConnectionConfig)
        assert conn.adapter == "postgres"
        assert conn.auto is False
        assert conn.include == ("*",)
        assert conn.exclude == ()
        assert conn.max_age_days == 7
        assert conn.statistics.enumeration_threshold == 50
        assert conn.statistics.percentiles == (1, 25, 50, 75, 99)

    def test_defaults_cascade_into_connection(self, tmp_path: Path) -> None:
        _write_config(
            tmp_path,
            """
defaults:
  max_age_days: 30
  statistics:
    top_n_values: 99
connections:
  primary:
    adapter: postgres
""",
        )
        cfg = load_project(tmp_path)
        conn = cfg.connections["primary"]
        assert conn.max_age_days == 30
        assert conn.statistics.top_n_values == 99

    def test_per_connection_overrides_defaults(self, tmp_path: Path) -> None:
        _write_config(
            tmp_path,
            """
defaults:
  max_age_days: 30
  statistics:
    top_n_values: 99
connections:
  primary:
    adapter: postgres
    max_age_days: 1
    statistics:
      top_n_values: 10
""",
        )
        cfg = load_project(tmp_path)
        conn = cfg.connections["primary"]
        assert conn.max_age_days == 1
        assert conn.statistics.top_n_values == 10

    def test_diff_thresholds_deep_merge(self, tmp_path: Path) -> None:
        _write_config(
            tmp_path,
            """
defaults:
  diff:
    stat_change_threshold:
      cardinality_ratio: 0.5
connections:
  primary:
    adapter: postgres
    diff:
      stat_change_threshold:
        percentile_pct: 0.99
""",
        )
        cfg = load_project(tmp_path)
        thresholds = cfg.connections["primary"].diff.stat_change_threshold
        assert thresholds["cardinality_ratio"] == 0.5
        assert thresholds["percentile_pct"] == 0.99
        # Unspecified key falls back to the built-in default.
        assert thresholds["default"] == 0.01

    def test_output_path_resolved_against_project_root(self, tmp_path: Path) -> None:
        _write_config(
            tmp_path,
            """
connections:
  primary:
    adapter: postgres
    output: my_prints
""",
        )
        cfg = load_project(tmp_path)
        assert cfg.connections["primary"].output == (tmp_path / "my_prints").resolve()

    def test_selector_patterns_lowercased(self, tmp_path: Path) -> None:
        _write_config(
            tmp_path,
            """
connections:
  primary:
    adapter: postgres
    include:
      - "Fixture.PUBLIC.*"
""",
        )
        cfg = load_project(tmp_path)
        assert cfg.connections["primary"].include == ("fixture.public.*",)

    def test_auto_flag_propagates(self, tmp_path: Path) -> None:
        _write_config(
            tmp_path,
            """
connections:
  primary:
    adapter: postgres
    auto: true
""",
        )
        cfg = load_project(tmp_path)
        assert cfg.connections["primary"].auto is True


class TestValidation:
    def test_invalid_adapter_rejected(self, tmp_path: Path) -> None:
        _write_config(
            tmp_path,
            """
connections:
  primary:
    adapter: oracle
""",
        )

        with pytest.raises(ConfigError, match="adapter must be one of"):
            load_project(tmp_path)

    def test_no_connections_rejected(self, tmp_path: Path) -> None:
        _write_config(tmp_path, "connections: {}\n")

        with pytest.raises(ConfigError, match="at least one connection"):
            load_project(tmp_path)

    def test_invalid_yaml_rejected(self, tmp_path: Path) -> None:
        _write_config(tmp_path, ":\n  invalid: : :\n")

        with pytest.raises(ConfigError, match="invalid YAML"):
            load_project(tmp_path)

    def test_percentile_out_of_range_rejected(self, tmp_path: Path) -> None:
        _write_config(
            tmp_path,
            """
connections:
  primary:
    adapter: postgres
    statistics:
      percentiles: [0, 50, 100]
""",
        )

        with pytest.raises(ConfigError, match="percentile 0 out of range"):
            load_project(tmp_path)

    def test_fractional_percentile_rejected(self, tmp_path: Path) -> None:
        _write_config(
            tmp_path,
            """
connections:
  primary:
    adapter: postgres
    statistics:
      percentiles: [0.5, 25, 50]
""",
        )

        with pytest.raises(ConfigError, match="is not an integer"):
            load_project(tmp_path)


class TestStatisticsIntegersAreLocated:
    """The same malformed value must not report three ways depending on its block."""

    @pytest.mark.parametrize(
        "value",
        ['"twenty"', "true", "20.0", "[20]"],
        ids=["string", "boolean", "float", "list"],
    )
    def test_a_bad_connection_value_names_file_connection_and_key(
        self,
        tmp_path: Path,
        value: str,
    ) -> None:
        _write_config(
            tmp_path,
            f"connections:\n  w:\n    adapter: postgres\n    statistics:\n"
            f"      top_n_values: {value}\n",
        )

        with pytest.raises(ConfigError) as exc:
            load_project(tmp_path)

        message = str(exc.value)
        assert ".dbprint.yaml" in message
        assert "connection 'w'" in message
        assert "top_n_values" in message

    def test_a_bad_defaults_value_names_the_defaults_block(self, tmp_path: Path) -> None:
        """The merge happens after validation, so the message names where the value was written."""

        _write_config(
            tmp_path,
            'defaults:\n  statistics:\n    top_n_values: "twenty"\n'
            "connections:\n  w:\n    adapter: postgres\n",
        )

        with pytest.raises(ConfigError, match=r"defaults\.statistics\.top_n_values"):
            load_project(tmp_path)

    def test_a_non_mapping_statistics_block_is_located(self, tmp_path: Path) -> None:
        _write_config(
            tmp_path,
            "connections:\n  w:\n    adapter: postgres\n    statistics: twenty\n",
        )

        with pytest.raises(ConfigError, match="statistics must be a mapping"):
            load_project(tmp_path)

    def test_the_defaults_cascade_still_resolves(self, tmp_path: Path) -> None:
        _write_config(
            tmp_path,
            "defaults:\n  statistics:\n    top_n_values: 99\n    enumeration_threshold: 10\n"
            "connections:\n  w:\n    adapter: postgres\n    statistics:\n      top_n_values: 5\n",
        )

        stats = load_project(tmp_path).connections["w"].statistics
        assert stats.top_n_values == 5
        assert stats.enumeration_threshold == 10


class TestFreshnessThresholdIsBounded:
    """A negative threshold is refused wherever it is written; zero is legal."""

    @pytest.mark.parametrize(
        "body",
        [
            "connections:\n  w:\n    adapter: postgres\n    max_age_days: -7\n",
            "defaults:\n  max_age_days: -7\nconnections:\n  w:\n    adapter: postgres\n",
            (
                "connections:\n  w:\n    adapter: postgres\n    rules:\n"
                '      - include: ["fixture.*"]\n        max_age_days: -1\n'
            ),
        ],
        ids=["connection", "defaults", "rule"],
    )
    def test_a_negative_threshold_is_refused(self, tmp_path: Path, body: str) -> None:
        _write_config(tmp_path, body)

        with pytest.raises(ConfigError, match="max_age_days"):
            load_project(tmp_path)

    def test_the_error_names_the_file_the_block_and_the_value(self, tmp_path: Path) -> None:
        _write_config(
            tmp_path,
            "defaults:\n  max_age_days: -7\nconnections:\n  w:\n    adapter: postgres\n",
        )

        with pytest.raises(ConfigError) as exc:
            load_project(tmp_path)

        message = str(exc.value)
        assert ".dbprint.yaml" in message
        assert "defaults: max_age_days" in message
        assert "-7" in message

    def test_a_rule_level_error_names_the_rule(self, tmp_path: Path) -> None:
        _write_config(
            tmp_path,
            "connections:\n  w:\n    adapter: postgres\n    rules:\n"
            '      - include: ["fixture.*"]\n        max_age_days: -1\n',
        )

        with pytest.raises(ConfigError, match=r"rules\[0\]\.max_age_days"):
            load_project(tmp_path)

    def test_a_negative_in_defaults_is_refused_even_when_overridden(self, tmp_path: Path) -> None:
        """The block holding the value is where it is wrong, whether or not it is read."""

        _write_config(
            tmp_path,
            "defaults:\n  max_age_days: -7\n"
            "connections:\n  w:\n    adapter: postgres\n    max_age_days: 7\n",
        )

        with pytest.raises(ConfigError, match="defaults: max_age_days"):
            load_project(tmp_path)

    @pytest.mark.parametrize(
        "value",
        ['"seven"', "true", "7.5", "[7]"],
        ids=["string", "boolean", "float", "list"],
    )
    def test_a_non_integer_threshold_is_refused(self, tmp_path: Path, value: str) -> None:
        _write_config(
            tmp_path,
            f"connections:\n  w:\n    adapter: postgres\n    max_age_days: {value}\n",
        )

        with pytest.raises(ConfigError, match="expected integer"):
            load_project(tmp_path)

    def test_zero_is_legal_at_every_level(self, tmp_path: Path) -> None:
        """Zero says re-extract on every run, which the skip comparison already produces."""

        _write_config(
            tmp_path,
            "defaults:\n  max_age_days: 0\n"
            "connections:\n  w:\n    adapter: postgres\n    rules:\n"
            '      - include: ["fixture.*"]\n        max_age_days: 0\n',
        )
        conn = load_project(tmp_path).connections["w"]

        assert conn.max_age_days == 0
        assert conn.settings_for("fixture.curation_event").max_age_days == 0

    def test_a_positive_and_an_absent_threshold_are_unchanged(self, tmp_path: Path) -> None:
        _write_config(
            tmp_path,
            "connections:\n  w:\n    adapter: postgres\n    max_age_days: 30\n"
            "  x:\n    adapter: postgres\n",
        )
        connections = load_project(tmp_path).connections

        assert connections["w"].max_age_days == 30
        assert connections["x"].max_age_days == 7


class TestReferenceExample:
    """The reference .dbprint.yaml in the format example must load cleanly."""

    def test_loads_reference_config(self) -> None:
        cfg = load_project(EXAMPLE_DIR)
        assert isinstance(cfg, ProjectConfig)
        assert "production" in cfg.connections
        conn = cfg.connections["production"]
        assert conn.adapter == "postgres"
        assert conn.auto is True
        assert conn.include == ("seedbank.*", "fixture.*")
        assert conn.exclude == ("seedbank.audit_*",)
        assert conn.max_age_days == 1
        assert isinstance(conn.statistics, StatisticsConfig)
        assert conn.statistics.top_n_values == 30
        assert conn.statistics.enumeration_threshold == 50
        assert isinstance(conn.diff, DiffConfig)


class TestRules:
    """Per-table profiling rules: a matcher plus the settings it overrides."""

    def test_absent_by_default(self, tmp_path: Path) -> None:
        root = _write_config(
            tmp_path,
            "connections:\n  w:\n    adapter: postgres\n",
        )
        conn = load_project(root).connections["w"]
        settings = conn.settings_for("any.table")

        assert conn.rules == ()
        assert settings.sample is None
        assert settings.filter is None
        assert settings.max_age_days == conn.max_age_days
        assert settings.statistics == conn.statistics
        assert settings.matched_rules == ()

    def test_defaults_rules_apply_to_every_connection(self, tmp_path: Path) -> None:
        root = _write_config(
            tmp_path,
            "defaults:\n  rules:\n    - sample: 0.01\nconnections:\n  w:\n    adapter: postgres\n",
        )

        assert load_project(root).connections["w"].settings_for("a.b").sample == 0.01

    def test_a_connection_rule_overrides_a_defaults_rule(self, tmp_path: Path) -> None:
        """Defaults rules are walked first, so a connection rule always wins."""

        root = _write_config(
            tmp_path,
            "defaults:\n"
            "  rules:\n"
            "    - sample: 0.01\n"
            "connections:\n"
            "  w:\n"
            "    adapter: postgres\n"
            "    rules:\n"
            "      - sample: 0.5\n",
        )

        assert load_project(root).connections["w"].settings_for("a.b").sample == 0.5

    def test_a_rule_applies_only_to_the_tables_it_matches(self, tmp_path: Path) -> None:
        root = _write_config(
            tmp_path,
            "connections:\n"
            "  w:\n"
            "    adapter: postgres\n"
            "    rules:\n"
            '      - include: ["fixture.*"]\n'
            "        sample: 0.25\n",
        )
        conn = load_project(root).connections["w"]

        assert conn.settings_for("fixture.curation_event").sample == 0.25
        assert conn.settings_for("public.curator").sample is None

    def test_a_rule_exclude_removes_tables_from_its_own_match(self, tmp_path: Path) -> None:
        root = _write_config(
            tmp_path,
            "connections:\n"
            "  w:\n"
            "    adapter: postgres\n"
            "    rules:\n"
            '      - include: ["fixture.*"]\n'
            '        exclude: ["fixture.curation_event_v2"]\n'
            "        sample: 0.25\n",
        )
        conn = load_project(root).connections["w"]

        assert conn.settings_for("fixture.curation_event").sample == 0.25
        assert conn.settings_for("fixture.curation_event_v2").sample is None

    def test_later_matching_rules_win_key_by_key(self, tmp_path: Path) -> None:
        """Declaration order decides, and an earlier rule's other keys survive."""

        root = _write_config(
            tmp_path,
            "connections:\n"
            "  w:\n"
            "    adapter: postgres\n"
            "    rules:\n"
            '      - include: ["*"]\n'
            "        filter: everything\n"
            "        max_age_days: 30\n"
            '      - include: ["fixture.curation_event"]\n'
            "        filter: narrow\n",
        )
        settings = load_project(root).connections["w"].settings_for("fixture.curation_event")

        assert settings.filter == "narrow"
        assert settings.max_age_days == 30

    def test_matched_rules_names_every_matching_rule_in_declaration_order(
        self,
        tmp_path: Path,
    ) -> None:
        root = _write_config(
            tmp_path,
            "connections:\n"
            "  w:\n"
            "    adapter: postgres\n"
            "    rules:\n"
            '      - include: ["*"]\n'
            "        filter: everything\n"
            '      - include: ["fixture.curation_event"]\n'
            "        filter: narrow\n",
        )
        settings = load_project(root).connections["w"].settings_for("fixture.curation_event")

        assert settings.matched_rules == (
            "connection 'w' rules[0]",
            "connection 'w' rules[1]",
        )

    def test_matched_rules_excludes_a_rule_that_does_not_match_the_table(
        self,
        tmp_path: Path,
    ) -> None:
        root = _write_config(
            tmp_path,
            "connections:\n"
            "  w:\n"
            "    adapter: postgres\n"
            "    rules:\n"
            '      - include: ["fixture.*"]\n'
            "        filter: narrow\n",
        )
        conn = load_project(root).connections["w"]

        assert conn.settings_for("fixture.curation_event").matched_rules == (
            "connection 'w' rules[0]",
        )
        assert conn.settings_for("public.curator").matched_rules == ()

    def test_declaration_order_decides_not_specificity(self, tmp_path: Path) -> None:
        """A broad rule declared last overrides a narrow one declared first."""

        root = _write_config(
            tmp_path,
            "connections:\n"
            "  w:\n"
            "    adapter: postgres\n"
            "    rules:\n"
            '      - include: ["fixture.curation_event"]\n'
            "        filter: narrow\n"
            '      - include: ["*"]\n'
            "        filter: everything\n",
        )

        assert (
            load_project(root).connections["w"].settings_for("fixture.curation_event").filter
            == "everything"
        )

    def test_statistics_merge_key_by_key_across_rules(self, tmp_path: Path) -> None:
        root = _write_config(
            tmp_path,
            "connections:\n"
            "  w:\n"
            "    adapter: postgres\n"
            "    rules:\n"
            '      - include: ["*"]\n'
            "        statistics: {top_n_values: 5}\n"
            '      - include: ["fixture.*"]\n'
            "        statistics: {percentiles: [50]}\n",
        )
        settings = load_project(root).connections["w"].settings_for("fixture.curation_event")

        assert settings.statistics.top_n_values == 5
        assert settings.statistics.percentiles == (50,)
        assert settings.statistics.enumeration_threshold == 50

    def test_max_age_days_resolves_per_table(self, tmp_path: Path) -> None:
        root = _write_config(
            tmp_path,
            "connections:\n"
            "  w:\n"
            "    adapter: postgres\n"
            "    max_age_days: 7\n"
            "    rules:\n"
            '      - include: ["fixture.archived_*"]\n'
            "        max_age_days: 1\n",
        )
        conn = load_project(root).connections["w"]

        assert conn.settings_for("fixture.archived_curator").max_age_days == 1
        assert conn.settings_for("fixture.curation_event").max_age_days == 7

    def test_rule_selectors_are_lowercased_like_include_and_exclude(self, tmp_path: Path) -> None:
        root = _write_config(
            tmp_path,
            "connections:\n"
            "  w:\n"
            "    adapter: postgres\n"
            "    rules:\n"
            '      - include: ["Fixture.Curation_Event"]\n'
            "        filter: predicate\n",
        )

        assert load_project(root).connections["w"].settings_for("fixture.curation_event").filter

    @pytest.mark.parametrize("bad", ["0", "1.5", "-0.2", "'half'", "true"])
    def test_sample_outside_the_unit_interval_is_rejected(self, tmp_path: Path, bad: str) -> None:
        root = _write_config(
            tmp_path,
            f"connections:\n  w:\n    adapter: postgres\n    rules:\n      - sample: {bad}\n",
        )

        with pytest.raises(ConfigError, match=r"rules\[0\].sample"):
            load_project(root)

    def test_sample_of_one_is_accepted(self, tmp_path: Path) -> None:
        """The interval is (0, 1]: a whole-table sample is degenerate, not invalid."""

        root = _write_config(
            tmp_path,
            "connections:\n  w:\n    adapter: postgres\n    rules:\n      - sample: 1\n",
        )

        assert load_project(root).connections["w"].settings_for("a.b").sample == 1.0

    @pytest.mark.parametrize("bad", ["''", "null", "3"], ids=["empty", "null", "number"])
    def test_filter_must_be_a_non_empty_string(self, tmp_path: Path, bad: str) -> None:
        root = _write_config(
            tmp_path,
            f"connections:\n  w:\n    adapter: postgres\n    rules:\n      - filter: {bad}\n",
        )

        with pytest.raises(ConfigError, match=r"rules\[0\]"):
            load_project(root)

    def test_rules_must_be_a_list(self, tmp_path: Path) -> None:
        root = _write_config(
            tmp_path,
            "connections:\n  w:\n    adapter: postgres\n    rules:\n      sample: 0.5\n",
        )

        with pytest.raises(ConfigError, match="rules must be a list"):
            load_project(root)

    def test_a_rule_that_changes_nothing_is_rejected(self, tmp_path: Path) -> None:
        """Otherwise a mis-nested key loads clean and silently does nothing."""

        root = _write_config(
            tmp_path,
            'connections:\n  w:\n    adapter: postgres\n    rules:\n      - include: ["a.*"]\n',
        )

        with pytest.raises(ConfigError, match="would do nothing"):
            load_project(root)

    def test_a_rule_that_matches_nothing_is_rejected(self, tmp_path: Path) -> None:
        """The mirror of the empty rule - it sets something but can never fire."""

        root = _write_config(
            tmp_path,
            "connections:\n  w:\n    adapter: postgres\n    rules:\n"
            "      - include: []\n        sample: 0.5\n",
        )

        with pytest.raises(ConfigError, match=r"rules\[0\]\.include is an empty list"):
            load_project(root)

    def test_an_omitted_include_matches_every_table(self, tmp_path: Path) -> None:
        root = _write_config(
            tmp_path,
            "connections:\n  w:\n    adapter: postgres\n    rules:\n      - sample: 0.5\n",
        )

        rule = load_project(root).connections["w"].rules[0]
        assert rule.include == ("*",)
        assert rule.matches("anything.at.all")

    def test_an_explicit_wildcard_include_is_accepted(self, tmp_path: Path) -> None:
        root = _write_config(
            tmp_path,
            "connections:\n  w:\n    adapter: postgres\n    rules:\n"
            '      - include: ["*"]\n        sample: 0.5\n',
        )

        assert load_project(root).connections["w"].rules[0].include == ("*",)

    def test_an_empty_exclude_removes_nothing(self, tmp_path: Path) -> None:
        root = _write_config(
            tmp_path,
            "connections:\n  w:\n    adapter: postgres\n    rules:\n"
            '      - include: ["a.*"]\n        exclude: []\n        sample: 0.5\n',
        )

        rule = load_project(root).connections["w"].rules[0]
        assert rule.exclude == ()
        assert rule.matches("a.b")

    def test_a_scope_block_is_rejected(self, tmp_path: Path) -> None:
        """Unknown keys are dropped silently, so this one has to be named."""

        root = _write_config(
            tmp_path,
            "connections:\n  w:\n    adapter: postgres\n    scope:\n      sample: 0.01\n",
        )

        with pytest.raises(ConfigError, match="`scope` is not read here"):
            load_project(root)

    def test_a_scope_block_is_rejected_in_defaults(self, tmp_path: Path) -> None:
        root = _write_config(
            tmp_path,
            "defaults:\n  scope:\n    sample: 0.01\nconnections:\n  w:\n    adapter: postgres\n",
        )

        with pytest.raises(ConfigError, match="`scope` is not read here"):
            load_project(root)

    @pytest.mark.parametrize(
        ("key", "value"),
        [("sample", "0.5"), ("filter", '"x > 1"'), ("min_rows", "1000")],
    )
    def test_a_rule_key_hoisted_onto_the_connection_is_rejected(
        self,
        tmp_path: Path,
        key: str,
        value: str,
    ) -> None:
        """Following the `scope` error and hoisting one level too high must not load clean."""

        root = _write_config(
            tmp_path,
            f"connections:\n  w:\n    adapter: postgres\n    {key}: {value}\n",
        )

        with pytest.raises(ConfigError, match=r"rules:"):
            load_project(root)

    @pytest.mark.parametrize(
        ("key", "value"),
        [("sample", "0.5"), ("filter", '"x > 1"'), ("min_rows", "1000")],
    )
    def test_a_rule_key_hoisted_into_defaults_is_rejected(
        self,
        tmp_path: Path,
        key: str,
        value: str,
    ) -> None:
        root = _write_config(
            tmp_path,
            f"defaults:\n  {key}: {value}\nconnections:\n  w:\n    adapter: postgres\n",
        )

        with pytest.raises(ConfigError, match="defaults"):
            load_project(root)

    def test_an_unrelated_unknown_key_is_still_ignored(self, tmp_path: Path) -> None:
        """The deny list is closed; the general ignore-unknown-keys policy is unchanged."""

        root = _write_config(
            tmp_path,
            "connections:\n  w:\n    adapter: postgres\n    smaple: 0.5\n",
        )

        assert load_project(root).connections["w"].adapter == "postgres"

    def test_connection_level_statistics_and_max_age_are_still_read(self, tmp_path: Path) -> None:
        """Both legitimately live at this level and must not join the deny list."""

        root = _write_config(
            tmp_path,
            "connections:\n  w:\n    adapter: postgres\n    max_age_days: 3\n"
            "    statistics:\n      top_n_values: 5\n",
        )

        conn = load_project(root).connections["w"]
        assert conn.max_age_days == 3
        assert conn.statistics.top_n_values == 5


class TestSizeConditions:
    """`min_rows` is a third matcher field, so it gates every setting its rule carries."""

    def test_a_rule_applies_only_to_tables_over_its_bar(self, tmp_path: Path) -> None:
        conn = _size_rule_config(tmp_path)

        assert conn.settings_for("fixture.curation_event", 800_000_000).sample == 0.01
        assert conn.settings_for("fixture.curation_event", 1000).sample is None

    def test_the_bar_is_inclusive(self, tmp_path: Path) -> None:
        conn = _size_rule_config(tmp_path)

        assert conn.settings_for("fixture.curation_event", 500_000_000).sample == 0.01

    def test_the_name_matcher_still_applies(self, tmp_path: Path) -> None:
        """Both conditions hold or the rule does not govern the table."""

        conn = _size_rule_config(tmp_path)

        assert conn.settings_for("other.curation_event", 900_000_000).sample is None

    def test_an_unavailable_estimate_leaves_the_rule_unapplied(self, tmp_path: Path) -> None:
        conn = _size_rule_config(tmp_path)

        assert conn.settings_for("fixture.curation_event").sample is None

    def test_a_known_empty_table_fails_the_bar_rather_than_going_unjudged(
        self,
        tmp_path: Path,
    ) -> None:
        """`0` is a size the catalog knows; it is not the absent estimate."""

        conn = _size_rule_config(tmp_path)

        assert conn.settings_for("fixture.curation_event", 0).sample is None

    def test_the_bar_gates_every_setting_the_rule_carries(self, tmp_path: Path) -> None:
        root = _write_config(
            tmp_path,
            "connections:\n"
            "  w:\n"
            "    adapter: postgres\n"
            "    max_age_days: 7\n"
            "    rules:\n"
            '      - include: ["fixture.*"]\n'
            "        min_rows: 1000\n"
            "        max_age_days: 30\n",
        )
        conn = load_project(root).connections["w"]

        assert conn.settings_for("fixture.curation_event", 5000).max_age_days == 30
        assert conn.settings_for("fixture.curation_event", 10).max_age_days == 7

    def test_a_rule_carrying_only_a_size_condition_is_rejected(self, tmp_path: Path) -> None:
        """A matcher is not a setting: it selects tables and overrides nothing on them."""

        root = _write_config(
            tmp_path,
            "connections:\n  w:\n    adapter: postgres\n    rules:\n      - min_rows: 1000\n",
        )

        with pytest.raises(ConfigError, match="would do nothing"):
            load_project(root)

    @pytest.mark.parametrize("bad", ["0", "-1", "true", "'many'", "1.5"])
    def test_a_bar_that_is_not_a_positive_integer_is_rejected(
        self,
        tmp_path: Path,
        bad: str,
    ) -> None:
        root = _write_config(
            tmp_path,
            f"connections:\n  w:\n    adapter: postgres\n    rules:\n"
            f"      - min_rows: {bad}\n        sample: 0.5\n",
        )

        with pytest.raises(ConfigError, match=r"rules\[0\].min_rows"):
            load_project(root)

    def test_the_hoisting_error_suggests_a_rule_that_would_load(self, tmp_path: Path) -> None:
        """The remediation carries a setting - a rule of bare `min_rows` is itself rejected."""

        root = _write_config(
            tmp_path,
            "connections:\n  w:\n    adapter: postgres\n    min_rows: 1000\n",
        )

        with pytest.raises(ConfigError) as excinfo:
            load_project(root)

        assert "sample: 0.01" in str(excinfo.value)

    def test_a_cascade_without_a_size_condition_reports_it_reads_no_row_counts(
        self,
        tmp_path: Path,
    ) -> None:
        root = _write_config(
            tmp_path,
            "connections:\n"
            "  w:\n"
            "    adapter: postgres\n"
            "    rules:\n"
            '      - include: ["fixture.*"]\n'
            "        sample: 0.5\n",
        )

        assert not load_project(root).connections["w"].rules_read_row_counts

    def test_a_cascade_with_a_size_condition_reports_it_reads_row_counts(
        self,
        tmp_path: Path,
    ) -> None:
        assert _size_rule_config(tmp_path).rules_read_row_counts

    def test_only_tables_a_size_rule_names_are_in_the_running_for_one(
        self,
        tmp_path: Path,
    ) -> None:
        conn = _size_rule_config(tmp_path)

        assert conn.size_conditions_name("fixture.curation_event")
        assert not conn.size_conditions_name("other.curation_event")

    def test_a_table_named_only_by_a_sizeless_rule_is_not_in_the_running(
        self,
        tmp_path: Path,
    ) -> None:
        root = _write_config(
            tmp_path,
            "connections:\n"
            "  w:\n"
            "    adapter: postgres\n"
            "    rules:\n"
            '      - include: ["fixture.*"]\n'
            "        min_rows: 1000\n"
            "        sample: 0.01\n"
            '      - include: ["other.*"]\n'
            "        sample: 0.5\n",
        )
        conn = load_project(root).connections["w"]

        assert not conn.size_conditions_name("other.curation_event")


def _size_rule_config(tmp_path: Path) -> ConnectionConfig:
    root = _write_config(
        tmp_path,
        "connections:\n"
        "  w:\n"
        "    adapter: postgres\n"
        "    rules:\n"
        '      - include: ["fixture.*"]\n'
        "        min_rows: 500000000\n"
        "        sample: 0.01\n",
    )

    return load_project(root).connections["w"]


class TestMaxRowsScanned:
    """A row-count ceiling: the operator states rows, `settings_for` derives the fraction."""

    def test_a_table_over_the_ceiling_is_narrowed(self, tmp_path: Path) -> None:
        root = _write_config(
            tmp_path,
            "connections:\n  w:\n    adapter: postgres\n    max_rows_scanned: 1000000000\n",
        )
        settings = load_project(root).connections["w"].settings_for("a.b", 10_000_000_000)

        assert settings.sample is not None
        assert settings.sample * 10_000_000_000 <= 1_000_000_000

    def test_a_table_under_the_ceiling_is_read_whole(self, tmp_path: Path) -> None:
        root = _write_config(
            tmp_path,
            "connections:\n  w:\n    adapter: postgres\n    max_rows_scanned: 1000000000\n",
        )

        assert load_project(root).connections["w"].settings_for("a.b", 200_000_000).sample is None

    def test_an_estimate_at_the_ceiling_is_read_whole(self, tmp_path: Path) -> None:
        """At or above the cap is "no narrowing", not a fraction of 1.0 (SPEC 2.2.8)."""

        root = _write_config(
            tmp_path,
            "connections:\n  w:\n    adapter: postgres\n    max_rows_scanned: 1000000000\n",
        )

        assert load_project(root).connections["w"].settings_for("a.b", 1_000_000_000).sample is None

    def test_small_drift_leaves_the_fraction_unchanged(self, tmp_path: Path) -> None:
        root = _write_config(
            tmp_path,
            "connections:\n  w:\n    adapter: postgres\n    max_rows_scanned: 1000000000\n",
        )
        conn = load_project(root).connections["w"]

        before = conn.settings_for("a.b", 10_000_000_000).sample
        after = conn.settings_for("a.b", 10_300_000_000).sample

        assert before == after

    def test_a_genuine_size_move_steps_the_fraction(self, tmp_path: Path) -> None:
        root = _write_config(
            tmp_path,
            "connections:\n  w:\n    adapter: postgres\n    max_rows_scanned: 1000000000\n",
        )
        conn = load_project(root).connections["w"]

        before = conn.settings_for("a.b", 10_000_000_000).sample
        after = conn.settings_for("a.b", 20_000_000_000).sample

        assert before != after

    def test_a_missing_estimate_leaves_the_table_unnarrowed(self, tmp_path: Path) -> None:
        root = _write_config(
            tmp_path,
            "connections:\n  w:\n    adapter: postgres\n    max_rows_scanned: 1000000000\n",
        )

        assert load_project(root).connections["w"].settings_for("a.b").sample is None

    def test_valid_at_rule_connection_and_defaults_level(self, tmp_path: Path) -> None:
        root = _write_config(
            tmp_path,
            "defaults:\n"
            "  max_rows_scanned: 2000000000\n"
            "connections:\n"
            "  w:\n"
            "    adapter: postgres\n"
            "    rules:\n"
            '      - include: ["fixture.*"]\n'
            "        max_rows_scanned: 500000000\n",
        )
        conn = load_project(root).connections["w"]

        assert conn.settings_for("fixture.curation_event", 1_000_000_000).sample is not None
        assert conn.settings_for("other.curation_event", 5_000_000_000).sample is not None

    def test_a_later_sample_beats_an_earlier_ceiling(self, tmp_path: Path) -> None:
        root = _write_config(
            tmp_path,
            "connections:\n"
            "  w:\n"
            "    adapter: postgres\n"
            "    max_rows_scanned: 1000000000\n"
            "    rules:\n"
            '      - include: ["*"]\n'
            "        sample: 0.5\n",
        )

        assert load_project(root).connections["w"].settings_for("a.b", 10_000_000_000).sample == 0.5

    def test_a_later_ceiling_beats_an_earlier_explicit_sample(self, tmp_path: Path) -> None:
        """Declaration order alone decides - neither directive has fixed priority."""

        root = _write_config(
            tmp_path,
            "connections:\n"
            "  w:\n"
            "    adapter: postgres\n"
            "    rules:\n"
            '      - include: ["*"]\n'
            "        sample: 0.5\n"
            '      - include: ["*"]\n'
            "        max_rows_scanned: 1000000000\n",
        )
        settings = load_project(root).connections["w"].settings_for("a.b", 10_000_000_000)

        assert settings.sample is not None
        assert settings.sample != 0.5
        assert settings.sample * 10_000_000_000 <= 1_000_000_000

    def test_the_same_rule_setting_both_prefers_the_explicit_sample(self, tmp_path: Path) -> None:
        root = _write_config(
            tmp_path,
            "connections:\n"
            "  w:\n"
            "    adapter: postgres\n"
            "    rules:\n"
            '      - include: ["*"]\n'
            "        sample: 0.5\n"
            "        max_rows_scanned: 1000000000\n",
        )

        assert load_project(root).connections["w"].settings_for("a.b", 10_000_000_000).sample == 0.5

    def test_a_ceiling_yields_to_a_filter_rather_than_colliding(self, tmp_path: Path) -> None:
        """Unlike sample+filter, a ceiling meeting a filter is not a load-time refusal."""

        root = _write_config(
            tmp_path,
            "connections:\n"
            "  w:\n"
            "    adapter: postgres\n"
            "    max_rows_scanned: 1000000000\n"
            "    rules:\n"
            '      - include: ["*"]\n'
            "        filter: created_at >= current_date - interval '30 days'\n",
        )
        settings = load_project(root).connections["w"].settings_for("a.b", 10_000_000_000)

        assert settings.filter is not None
        assert settings.sample is None
        assert settings.ceiling_yielded is True

    def test_a_filter_with_no_ceiling_does_not_report_a_yield(self, tmp_path: Path) -> None:
        root = _write_config(
            tmp_path,
            "connections:\n"
            "  w:\n"
            "    adapter: postgres\n"
            "    rules:\n"
            '      - include: ["*"]\n'
            "        filter: x > 1\n",
        )

        assert not load_project(root).connections["w"].settings_for("a.b").ceiling_yielded

    def test_composed_with_min_rows_the_two_conditions_are_orthogonal(self, tmp_path: Path) -> None:
        root = _write_config(
            tmp_path,
            "connections:\n"
            "  w:\n"
            "    adapter: postgres\n"
            "    rules:\n"
            '      - include: ["fixture.*"]\n'
            "        min_rows: 500000000\n"
            "        max_rows_scanned: 1000000000\n",
        )
        conn = load_project(root).connections["w"]

        assert conn.settings_for("fixture.curation_event", 10_000_000_000).sample is not None
        assert conn.settings_for("fixture.curation_event", 1000).sample is None

    def test_a_config_with_neither_ceiling_nor_min_rows_reads_no_row_counts(
        self,
        tmp_path: Path,
    ) -> None:
        root = _write_config(
            tmp_path,
            "connections:\n  w:\n    adapter: postgres\n",
        )

        assert not load_project(root).connections["w"].rules_read_row_counts

    def test_a_connection_level_ceiling_alone_reads_row_counts(self, tmp_path: Path) -> None:
        root = _write_config(
            tmp_path,
            "connections:\n  w:\n    adapter: postgres\n    max_rows_scanned: 1000000000\n",
        )

        assert load_project(root).connections["w"].rules_read_row_counts

    def test_a_connection_level_ceiling_names_every_table(self, tmp_path: Path) -> None:
        root = _write_config(
            tmp_path,
            "connections:\n  w:\n    adapter: postgres\n    max_rows_scanned: 1000000000\n",
        )
        conn = load_project(root).connections["w"]

        assert conn.size_conditions_name("anything.at_all")

    @pytest.mark.parametrize("bad", ["0", "-1", "true", "'many'", "1.5"])
    def test_a_ceiling_that_is_not_a_positive_integer_is_rejected(
        self,
        tmp_path: Path,
        bad: str,
    ) -> None:
        root = _write_config(
            tmp_path,
            f"connections:\n  w:\n    adapter: postgres\n    max_rows_scanned: {bad}\n",
        )

        with pytest.raises(ConfigError, match=r"connection 'w': max_rows_scanned"):
            load_project(root)

    def test_a_rule_ceiling_that_is_not_a_positive_integer_names_the_rule(
        self,
        tmp_path: Path,
    ) -> None:
        root = _write_config(
            tmp_path,
            "connections:\n  w:\n    adapter: postgres\n    rules:\n      - max_rows_scanned: 0\n",
        )

        with pytest.raises(ConfigError, match=r"rules\[0\]\.max_rows_scanned"):
            load_project(root)

    def test_a_rule_carrying_only_a_ceiling_is_accepted(self, tmp_path: Path) -> None:
        """Unlike `min_rows`, a ceiling is a setting on its own - it needs no partner key."""

        root = _write_config(
            tmp_path,
            "connections:\n  w:\n    adapter: postgres\n    rules:\n"
            "      - max_rows_scanned: 1000000000\n",
        )

        rules = load_project(root).connections["w"].rules
        assert len(rules) == 1
        assert rules[0].max_rows_scanned == 1000000000

    def test_a_rule_carrying_nothing_at_all_is_still_rejected(self, tmp_path: Path) -> None:
        root = _write_config(
            tmp_path,
            'connections:\n  w:\n    adapter: postgres\n    rules:\n      - include: ["*"]\n',
        )

        with pytest.raises(ConfigError, match="max_rows_scanned, so it would do nothing"):
            load_project(root)


class TestRuleErrorsNameTheRule:
    """Rule keys reuse the connection's coercion helpers, so errors must still name the rule."""

    def test_rule_include_names_the_rule(self, tmp_path: Path) -> None:
        root = _write_config(
            tmp_path,
            "connections:\n  w:\n    adapter: postgres\n    rules:\n"
            '      - include: ["a.*"]\n        sample: 0.5\n'
            '      - include: "a.b"\n        sample: 0.5\n',
        )

        with pytest.raises(ConfigError, match=r"rules\[1\]\.include must be a list of strings"):
            load_project(root)

    def test_rule_exclude_names_the_rule(self, tmp_path: Path) -> None:
        root = _write_config(
            tmp_path,
            "connections:\n  w:\n    adapter: postgres\n    rules:\n"
            '      - include: ["a.*"]\n        sample: 0.5\n'
            '      - exclude: "a.b"\n        sample: 0.5\n',
        )

        with pytest.raises(ConfigError, match=r"rules\[1\]\.exclude must be a list of strings"):
            load_project(root)

    def test_rule_percentiles_name_the_rule(self, tmp_path: Path) -> None:
        root = _write_config(
            tmp_path,
            "connections:\n  w:\n    adapter: postgres\n    rules:\n"
            "      - statistics:\n          percentiles: [0.5]\n",
        )

        with pytest.raises(ConfigError, match=r"rules\[0\]\.statistics\.percentiles"):
            load_project(root)

    def test_a_defaults_rule_names_the_list_it_came_from(self, tmp_path: Path) -> None:
        root = _write_config(
            tmp_path,
            'defaults:\n  rules:\n    - include: "a.b"\n      sample: 0.5\n'
            "connections:\n  w:\n    adapter: postgres\n",
        )

        with pytest.raises(ConfigError, match=r"defaults rules\[0\]\.include"):
            load_project(root)

    def test_a_connection_selector_names_only_itself(self, tmp_path: Path) -> None:
        root = _write_config(
            tmp_path,
            'connections:\n  w:\n    adapter: postgres\n    include: "a.b"\n',
        )

        with pytest.raises(ConfigError) as exc:
            load_project(root)

        message = str(exc.value)
        assert "include must be a list of strings" in message
        assert "rules[" not in message

    def test_a_connection_percentile_names_only_itself(self, tmp_path: Path) -> None:
        root = _write_config(
            tmp_path,
            "connections:\n  w:\n    adapter: postgres\n    statistics:\n      percentiles: [0.5]\n",
        )

        with pytest.raises(ConfigError) as exc:
            load_project(root)

        message = str(exc.value)
        assert "percentile 0.5 is not an integer" in message
        assert "rules[" not in message
        assert "statistics.percentiles" not in message


class TestNarrowingIsExclusive:
    """A table is narrowed by a predicate or by a fraction, never both (SPEC 2.2.8).

    One rule carrying the pair is caught at load; a pair split across rules only at resolution.
    """

    def test_one_rule_carrying_both_is_rejected_at_load(self, tmp_path: Path) -> None:
        root = _write_config(
            tmp_path,
            "connections:\n"
            "  w:\n"
            "    adapter: postgres\n"
            "    rules:\n"
            "      - sample: 0.01\n"
            "        filter: x > 1\n",
        )

        with pytest.raises(ConfigError, match=r"rules\[0\] sets both sample and filter"):
            load_project(root)

    def test_a_cascade_settling_on_both_is_rejected_when_the_table_resolves(
        self,
        tmp_path: Path,
    ) -> None:
        root = _write_config(
            tmp_path,
            "connections:\n"
            "  w:\n"
            "    adapter: postgres\n"
            "    rules:\n"
            '      - include: ["fixture.*"]\n'
            "        filter: x > 1\n"
            '      - include: ["fixture.curation_event"]\n'
            "        sample: 0.01\n",
        )
        conn = load_project(root).connections["w"]

        with pytest.raises(ConfigError) as exc:
            conn.settings_for("fixture.curation_event")

        message = str(exc.value)
        assert "fixture.curation_event" in message
        assert "rules[0]" in message
        assert "rules[1]" in message

    def test_the_later_rule_does_not_silently_replace_the_earlier_kind(
        self,
        tmp_path: Path,
    ) -> None:
        root = _write_config(
            tmp_path,
            "connections:\n"
            "  w:\n"
            "    adapter: postgres\n"
            "    rules:\n"
            "      - filter: x > 1\n"
            "      - sample: 0.01\n",
        )
        conn = load_project(root).connections["w"]

        with pytest.raises(ConfigError, match="never both"):
            conn.settings_for("a.b")

    def test_a_defaults_rule_and_a_connection_rule_are_told_apart(self, tmp_path: Path) -> None:
        root = _write_config(
            tmp_path,
            "defaults:\n"
            "  rules:\n"
            "    - filter: x > 1\n"
            "connections:\n"
            "  w:\n"
            "    adapter: postgres\n"
            "    rules:\n"
            "      - sample: 0.01\n",
        )
        conn = load_project(root).connections["w"]

        with pytest.raises(ConfigError) as exc:
            conn.settings_for("a.b")

        assert "defaults rules[0]" in str(exc.value)
        assert "connection 'w' rules[0]" in str(exc.value)

    def test_rules_carrying_one_key_each_over_different_tables_both_resolve(
        self,
        tmp_path: Path,
    ) -> None:
        root = _write_config(
            tmp_path,
            "connections:\n"
            "  w:\n"
            "    adapter: postgres\n"
            "    rules:\n"
            '      - include: ["fixture.curation_event"]\n'
            "        filter: x > 1\n"
            '      - include: ["fixture.viability_check"]\n'
            "        sample: 0.01\n",
        )
        conn = load_project(root).connections["w"]

        curation_event = conn.settings_for("fixture.curation_event")
        viability_check = conn.settings_for("fixture.viability_check")

        assert (curation_event.filter, curation_event.sample) == ("x > 1", None)
        assert (viability_check.filter, viability_check.sample) == (None, 0.01)


class TestRedactTargetsAreCheckedAgainstTheirVocabularies:
    """A rule naming no target is a load error; one naming an unknown target covered nothing.

    Both select no column, but a misspelled target would load clean and write the literals.
    """

    def test_an_unknown_sensitivity_is_rejected(self, tmp_path: Path) -> None:
        root = _write_config(
            tmp_path,
            "connections:\n  w:\n    adapter: postgres\n    redact:\n"
            "      - sensitivity: [personal_names]\n        with: drop\n",
        )

        with pytest.raises(ConfigError, match=r"redact\[0\]\.sensitivity"):
            load_project(root)

    def test_the_rejection_lists_the_accepted_values(self, tmp_path: Path) -> None:
        root = _write_config(
            tmp_path,
            "connections:\n  w:\n    adapter: postgres\n    redact:\n"
            "      - sensitivity: [personal_names]\n        with: drop\n",
        )

        with pytest.raises(ConfigError) as exc:
            load_project(root)

        message = str(exc.value)

        assert "'personal_name'" in message
        assert "'postal_address'" in message
        assert "'contact'" in message
        assert "'personal_names'" in message

    def test_an_unknown_looks_like_is_rejected(self, tmp_path: Path) -> None:
        root = _write_config(
            tmp_path,
            "connections:\n  w:\n    adapter: postgres\n    redact:\n"
            '      - looks_like: ["e-mail"]\n        with: hash\n',
        )

        with pytest.raises(ConfigError, match=r"redact\[0\]\.looks_like"):
            load_project(root)

    def test_a_rule_naming_ipv4_is_rejected(self, tmp_path: Path) -> None:
        """`ipv4` is not in the current vocabulary; a rule naming it is refused like any typo."""

        root = _write_config(
            tmp_path,
            "connections:\n  w:\n    adapter: postgres\n    redact:\n"
            '      - looks_like: ["ipv4"]\n        with: mask\n',
        )

        with pytest.raises(ConfigError, match=r"redact\[0\]\.looks_like") as excinfo:
            load_project(root)

        assert "'ipv4'" in str(excinfo.value)

    def test_a_partly_valid_rule_is_not_partly_applied(self, tmp_path: Path) -> None:
        """One bad target refuses the whole rule; the good target alongside it does not save it."""

        root = _write_config(
            tmp_path,
            "connections:\n  w:\n    adapter: postgres\n    redact:\n"
            '      - columns: ["*.curator.email"]\n        sensitivity: [typo]\n',
        )

        with pytest.raises(ConfigError, match=r"redact\[0\]\.sensitivity"):
            load_project(root)

    def test_case_and_the_accepted_values_still_agree(self, tmp_path: Path) -> None:
        """The vocabulary check runs after the lowercasing, so an upper-case value loads."""

        root = _write_config(
            tmp_path,
            "connections:\n  w:\n    adapter: postgres\n    redact:\n"
            "      - sensitivity: [PERSONAL_NAME]\n        with: drop\n",
        )

        assert load_project(root).connections["w"].redact[0].sensitivity == ("personal_name",)

    def test_every_member_of_each_vocabulary_is_accepted(self, tmp_path: Path) -> None:
        """The sets are read from the modules that define them, so both stay reachable."""

        sensitivities = ", ".join(sorted(get_args(Sensitivity)))
        patterns = ", ".join(sorted(get_args(LooksLike)))
        root = _write_config(
            tmp_path,
            "connections:\n  w:\n    adapter: postgres\n    redact:\n"
            f"      - sensitivity: [{sensitivities}]\n        with: drop\n"
            f"      - looks_like: [{patterns}]\n        with: mask\n",
        )
        redact = load_project(root).connections["w"].redact

        assert set(redact[0].sensitivity) == set(get_args(Sensitivity))
        assert set(redact[1].looks_like) == set(get_args(LooksLike))

    def test_columns_stays_an_open_vocabulary(self, tmp_path: Path) -> None:
        """A glob matching no table today may match one tomorrow, so it is not checked."""

        root = _write_config(
            tmp_path,
            "connections:\n  w:\n    adapter: postgres\n    redact:\n"
            '      - columns: ["*.no_such_table.no_such_column"]\n        with: mask\n',
        )

        assert load_project(root).connections["w"].redact[0].columns == (
            "*.no_such_table.no_such_column",
        )


class TestRedactCascadesFromDefaults:
    def test_a_defaults_rule_reaches_every_connection(self, tmp_path: Path) -> None:
        root = _write_config(
            tmp_path,
            "defaults:\n  redact:\n    - sensitivity: [personal_name]\n      with: drop\n"
            "connections:\n  w:\n    adapter: postgres\n  x:\n    adapter: mysql\n",
        )
        connections = load_project(root).connections

        for name in ("w", "x"):
            assert connections[name].redaction_for("a.b.c", "personal_name", None) == "drop"

    def test_defaults_rules_are_walked_before_the_connections_own(self, tmp_path: Path) -> None:
        root = _write_config(
            tmp_path,
            "defaults:\n  redact:\n    - sensitivity: [contact]\n      with: mask\n"
            "connections:\n  w:\n    adapter: postgres\n    redact:\n"
            "      - sensitivity: [contact]\n        with: hash\n",
        )
        conn = load_project(root).connections["w"]

        assert [r.with_ for r in conn.redact] == ["mask", "hash"]
        assert conn.redaction_for("a.b.c", "contact", None) == "hash"

    def test_a_connection_rule_cannot_lift_coverage_a_defaults_rule_set(
        self,
        tmp_path: Path,
    ) -> None:
        """No primitive means "not redacted", so the cascade fails closed."""

        root = _write_config(
            tmp_path,
            "defaults:\n  redact:\n    - sensitivity: [contact]\n      with: mask\n"
            "connections:\n  w:\n    adapter: postgres\n    redact:\n"
            '      - columns: ["a.b.other"]\n        with: drop\n',
        )
        conn = load_project(root).connections["w"]

        assert conn.redaction_for("a.b.c", "contact", None) == "mask"

    def test_a_defaults_rule_names_the_list_it_came_from(self, tmp_path: Path) -> None:
        root = _write_config(
            tmp_path,
            "defaults:\n  redact:\n    - sensitivity: [typo]\n      with: drop\n"
            "connections:\n  w:\n    adapter: postgres\n",
        )

        with pytest.raises(ConfigError, match=r"defaults redact\[0\]\.sensitivity"):
            load_project(root)

    def test_a_connection_rule_names_the_connection(self, tmp_path: Path) -> None:
        root = _write_config(
            tmp_path,
            "connections:\n  w:\n    adapter: postgres\n    redact:\n"
            "      - sensitivity: [typo]\n        with: drop\n",
        )

        with pytest.raises(ConfigError, match=r"connection 'w' redact\[0\]\.sensitivity"):
            load_project(root)

    def test_a_defaults_redact_that_is_not_a_list_is_rejected(self, tmp_path: Path) -> None:
        root = _write_config(
            tmp_path,
            "defaults:\n  redact:\n    sensitivity: [contact]\n"
            "connections:\n  w:\n    adapter: postgres\n",
        )

        with pytest.raises(ConfigError, match="must be a list of rule mappings"):
            load_project(root)

    def test_no_redact_anywhere_leaves_the_connection_empty(self, tmp_path: Path) -> None:
        root = _write_config(tmp_path, "connections:\n  w:\n    adapter: postgres\n")

        assert load_project(root).connections["w"].redact == ()


class TestStatChangeThresholdIsValidated:
    """A malformed threshold is refused at load, not left until the renderer's `float()` fails."""

    @pytest.mark.parametrize(
        "value",
        ['"0.02"', "true", "{a: 1}", "[0.02]"],
        ids=["string", "boolean", "mapping", "list"],
    )
    def test_a_non_numeric_value_names_file_connection_and_key(
        self,
        tmp_path: Path,
        value: str,
    ) -> None:
        _write_config(
            tmp_path,
            f"connections:\n  w:\n    adapter: postgres\n    diff:\n"
            f"      stat_change_threshold:\n        cardinality_ratio: {value}\n",
        )

        with pytest.raises(ConfigError) as exc:
            load_project(tmp_path)

        message = str(exc.value)
        assert ".dbprint.yaml" in message
        assert "connection 'w'" in message
        assert "cardinality_ratio" in message

    @pytest.mark.parametrize("value", ["-0.5", "2.0"], ids=["negative", "above one"])
    def test_a_value_outside_the_unit_interval_is_refused(
        self,
        tmp_path: Path,
        value: str,
    ) -> None:
        _write_config(
            tmp_path,
            f"connections:\n  w:\n    adapter: postgres\n    diff:\n"
            f"      stat_change_threshold:\n        default: {value}\n",
        )

        with pytest.raises(ConfigError, match=r"outside \[0, 1\]"):
            load_project(tmp_path)

    def test_an_unknown_key_is_named_with_the_accepted_set(self, tmp_path: Path) -> None:
        """The rename case: a key nothing reads silently substitutes `default`."""

        _write_config(
            tmp_path,
            "connections:\n  w:\n    adapter: postgres\n    diff:\n"
            "      stat_change_threshold:\n        top_values_coverage: 0.05\n",
        )

        with pytest.raises(ConfigError) as exc:
            load_project(tmp_path)

        message = str(exc.value)

        # Not `values_coverage`: the key merely contains it; assert the full list too.
        assert "top_values_coverage" in message
        assert "Accepted keys" in message
        assert "percentile_pct" in message

    def test_a_bad_defaults_value_names_the_defaults_block(self, tmp_path: Path) -> None:
        _write_config(
            tmp_path,
            'defaults:\n  diff:\n    stat_change_threshold:\n      default: "0.02"\n'
            "connections:\n  w:\n    adapter: postgres\n",
        )

        with pytest.raises(ConfigError, match=r"defaults\.diff\.stat_change_threshold\.default"):
            load_project(tmp_path)

    def test_a_non_mapping_block_is_located(self, tmp_path: Path) -> None:
        _write_config(
            tmp_path,
            "connections:\n  w:\n    adapter: postgres\n    diff:\n"
            "      stat_change_threshold: 0.02\n",
        )

        with pytest.raises(ConfigError, match="stat_change_threshold must be a mapping"):
            load_project(tmp_path)

    def test_a_non_mapping_diff_block_is_located(self, tmp_path: Path) -> None:
        _write_config(
            tmp_path,
            "connections:\n  w:\n    adapter: postgres\n    diff: 0.02\n",
        )

        with pytest.raises(ConfigError, match="diff must be a mapping"):
            load_project(tmp_path)

    def test_a_whole_number_is_a_well_formed_fraction(self, tmp_path: Path) -> None:
        """1 means "show every change", which is a threshold like any other."""

        _write_config(
            tmp_path,
            "connections:\n  w:\n    adapter: postgres\n    diff:\n"
            "      stat_change_threshold:\n        default: 1\n",
        )
        conn = load_project(tmp_path).connections["w"]

        assert conn.diff.stat_change_threshold["default"] == 1.0

    def test_a_valid_block_still_merges_over_the_spec_defaults(self, tmp_path: Path) -> None:
        _write_config(
            tmp_path,
            "connections:\n  w:\n    adapter: postgres\n    diff:\n"
            "      stat_change_threshold:\n        cardinality_ratio: 0.5\n",
        )
        thresholds = load_project(tmp_path).connections["w"].diff.stat_change_threshold

        assert thresholds["cardinality_ratio"] == 0.5
        assert thresholds["default"] == 0.01

    def test_an_absent_block_keeps_the_spec_defaults(self, tmp_path: Path) -> None:
        _write_config(tmp_path, "connections:\n  w:\n    adapter: postgres\n")
        conn = load_project(tmp_path).connections["w"]

        assert conn.diff.stat_change_threshold == {
            "cardinality_ratio": 0.02,
            "percentile_pct": 0.05,
            "values_coverage": 0.05,
            "default": 0.01,
        }


class TestFalsyBlocksAreRefused:
    """Falsy blocks are the same mistake as a truthy non-mapping; `None` stays the empty case."""

    @pytest.mark.parametrize(
        "value",
        ["0", '""', "[]", "false"],
        ids=["zero", "empty_string", "empty_list", "false"],
    )
    def test_a_falsy_diff_block_is_refused(self, tmp_path: Path, value: str) -> None:
        _write_config(tmp_path, f"connections:\n  w:\n    adapter: postgres\n    diff: {value}\n")

        with pytest.raises(ConfigError, match="diff must be a mapping"):
            load_project(tmp_path)

    @pytest.mark.parametrize(
        "value",
        ["0", '""', "[]", "false"],
        ids=["zero", "empty_string", "empty_list", "false"],
    )
    def test_a_falsy_defaults_diff_block_is_refused(self, tmp_path: Path, value: str) -> None:
        _write_config(
            tmp_path,
            f"defaults:\n  diff: {value}\nconnections:\n  w:\n    adapter: postgres\n",
        )

        with pytest.raises(ConfigError, match=r"defaults\.diff must be a mapping"):
            load_project(tmp_path)

    @pytest.mark.parametrize(
        "value",
        ["0", '""', "[]", "false"],
        ids=["zero", "empty_string", "empty_list", "false"],
    )
    def test_a_falsy_stat_change_threshold_is_refused(self, tmp_path: Path, value: str) -> None:
        _write_config(
            tmp_path,
            "connections:\n  w:\n    adapter: postgres\n    diff:\n"
            f"      stat_change_threshold: {value}\n",
        )

        with pytest.raises(ConfigError, match="stat_change_threshold must be a mapping"):
            load_project(tmp_path)

    @pytest.mark.parametrize(
        "value",
        ["0", '""', "[]", "false"],
        ids=["zero", "empty_string", "empty_list", "false"],
    )
    def test_a_falsy_defaults_stat_change_threshold_is_refused(
        self,
        tmp_path: Path,
        value: str,
    ) -> None:
        _write_config(
            tmp_path,
            f"defaults:\n  diff:\n    stat_change_threshold: {value}\n"
            "connections:\n  w:\n    adapter: postgres\n",
        )

        with pytest.raises(
            ConfigError,
            match=r"defaults\.diff\.stat_change_threshold must be a mapping",
        ):
            load_project(tmp_path)

    @pytest.mark.parametrize(
        "value",
        ["0", '""', "[]", "false"],
        ids=["zero", "empty_string", "empty_list", "false"],
    )
    def test_a_falsy_statistics_block_is_refused(self, tmp_path: Path, value: str) -> None:
        _write_config(
            tmp_path,
            f"connections:\n  w:\n    adapter: postgres\n    statistics: {value}\n",
        )

        with pytest.raises(ConfigError, match="statistics must be a mapping"):
            load_project(tmp_path)

    @pytest.mark.parametrize(
        "value",
        ["0", '""', "[]", "false"],
        ids=["zero", "empty_string", "empty_list", "false"],
    )
    def test_a_falsy_defaults_statistics_block_is_refused(self, tmp_path: Path, value: str) -> None:
        _write_config(
            tmp_path,
            f"defaults:\n  statistics: {value}\nconnections:\n  w:\n    adapter: postgres\n",
        )

        with pytest.raises(ConfigError, match=r"defaults\.statistics must be a mapping"):
            load_project(tmp_path)

    @pytest.mark.parametrize(
        "value",
        ["0", '""', "[]", "false"],
        ids=["zero", "empty_string", "empty_list", "false"],
    )
    def test_a_falsy_rule_statistics_block_is_refused(self, tmp_path: Path, value: str) -> None:
        _write_config(
            tmp_path,
            "connections:\n  w:\n    adapter: postgres\n    rules:\n"
            f'      - include: ["a.*"]\n        statistics: {value}\n',
        )

        with pytest.raises(ConfigError, match="statistics must be a mapping"):
            load_project(tmp_path)

    @pytest.mark.parametrize(
        "value",
        ["0", '""', "[]", "false"],
        ids=["zero", "empty_string", "empty_list", "false"],
    )
    def test_a_falsy_assertions_block_is_refused(self, tmp_path: Path, value: str) -> None:
        _write_config(
            tmp_path,
            f"connections:\n  w:\n    adapter: postgres\n    assertions: {value}\n",
        )

        with pytest.raises(ConfigError, match="assertions` must be a mapping"):
            load_project(tmp_path)

    def test_a_truthy_diff_non_mapping_message_is_unchanged(self, tmp_path: Path) -> None:
        """`diff: 0.02`'s refusal message must not move, byte for byte."""

        _write_config(tmp_path, "connections:\n  w:\n    adapter: postgres\n    diff: 0.02\n")

        with pytest.raises(ConfigError) as exc:
            load_project(tmp_path)

        assert str(exc.value).endswith(
            "connection 'w': connection 'w'.diff must be a mapping, got float.",
        )

    def test_null_and_empty_mapping_diff_load_clean(self, tmp_path: Path) -> None:
        """`diff:` and `diff: {}` mean the same as an absent block."""

        for body in (
            "connections:\n  w:\n    adapter: postgres\n    diff:\n",
            "connections:\n  w:\n    adapter: postgres\n    diff: {}\n",
        ):
            _write_config(tmp_path, body)
            diff = load_project(tmp_path).connections["w"].diff

            assert diff.stat_change_threshold == {
                "cardinality_ratio": 0.02,
                "percentile_pct": 0.05,
                "values_coverage": 0.05,
                "default": 0.01,
            }


class TestTopNNullPatterns:
    """How many null combinations a table publishes is its own budget, not the value cap's."""

    def test_an_absent_key_leaves_the_documented_default(self, tmp_path: Path) -> None:
        _write_config(tmp_path, "connections:\n  w:\n    adapter: postgres\n")

        assert load_project(tmp_path).connections["w"].statistics.top_n_null_patterns == 20

    def test_a_rule_can_raise_it_for_one_table(self, tmp_path: Path) -> None:
        _write_config(
            tmp_path,
            "connections:\n  w:\n    adapter: postgres\n    rules:\n"
            '      - include: ["public.wide"]\n'
            "        statistics:\n          top_n_null_patterns: 50\n",
        )
        conn = load_project(tmp_path).connections["w"]

        assert conn.settings_for("public.wide").statistics.top_n_null_patterns == 50
        assert conn.settings_for("public.other").statistics.top_n_null_patterns == 20

    def test_it_is_independent_of_the_value_cap(self, tmp_path: Path) -> None:
        _write_config(
            tmp_path,
            "connections:\n  w:\n    adapter: postgres\n    statistics:\n"
            "      top_n_values: 5\n      top_n_null_patterns: 40\n",
        )
        stats = load_project(tmp_path).connections["w"].statistics

        assert (stats.top_n_values, stats.top_n_null_patterns) == (5, 40)

    def test_a_non_integer_is_refused(self, tmp_path: Path) -> None:
        _write_config(
            tmp_path,
            "connections:\n  w:\n    adapter: postgres\n    statistics:\n"
            '      top_n_null_patterns: "many"\n',
        )

        with pytest.raises(ConfigError, match="top_n_null_patterns: expected integer"):
            load_project(tmp_path)


class TestMaterializeSample:
    """The one setting that lets the producer write, so its default matters too."""

    def test_an_absent_key_leaves_the_write_enabled(self, tmp_path: Path) -> None:
        _write_config(tmp_path, "connections:\n  w:\n    adapter: postgres\n")

        assert load_project(tmp_path).connections["w"].materialize_sample is True

    def test_a_connection_can_refuse_the_write(self, tmp_path: Path) -> None:
        _write_config(
            tmp_path,
            "connections:\n  w:\n    adapter: postgres\n    materialize_sample: false\n",
        )

        assert load_project(tmp_path).connections["w"].materialize_sample is False

    def test_a_connection_overrides_the_projects_refusal(self, tmp_path: Path) -> None:
        """`defaults` sets the policy; a connection that names the key wins over it."""

        _write_config(
            tmp_path,
            "defaults:\n  materialize_sample: false\n"
            "connections:\n"
            "  inherits:\n    adapter: postgres\n"
            "  overrides:\n    adapter: postgres\n    materialize_sample: true\n",
        )
        connections = load_project(tmp_path).connections

        assert connections["inherits"].materialize_sample is False
        assert connections["overrides"].materialize_sample is True

    def test_a_non_boolean_is_refused(self, tmp_path: Path) -> None:
        _write_config(
            tmp_path,
            "connections:\n  w:\n    adapter: postgres\n    materialize_sample: 1\n",
        )

        with pytest.raises(ConfigError, match="materialize_sample: expected true or false, got 1"):
            load_project(tmp_path)


class TestSketchAllColumns:
    """The setting that widens `sketch` to every sketchable column, so its default matters too."""

    def test_an_absent_key_leaves_the_narrower_set(self, tmp_path: Path) -> None:
        _write_config(tmp_path, "connections:\n  w:\n    adapter: postgres\n")

        assert load_project(tmp_path).connections["w"].sketch_all_columns is False

    def test_a_connection_can_widen_it(self, tmp_path: Path) -> None:
        _write_config(
            tmp_path,
            "connections:\n  w:\n    adapter: postgres\n    sketch_all_columns: true\n",
        )

        assert load_project(tmp_path).connections["w"].sketch_all_columns is True

    def test_a_connection_overrides_the_projects_default(self, tmp_path: Path) -> None:
        """`defaults` sets the policy; a connection that names the key wins over it."""

        _write_config(
            tmp_path,
            "defaults:\n  sketch_all_columns: true\n"
            "connections:\n"
            "  inherits:\n    adapter: postgres\n"
            "  overrides:\n    adapter: postgres\n    sketch_all_columns: false\n",
        )
        connections = load_project(tmp_path).connections

        assert connections["inherits"].sketch_all_columns is True
        assert connections["overrides"].sketch_all_columns is False

    def test_a_non_boolean_is_refused(self, tmp_path: Path) -> None:
        _write_config(
            tmp_path,
            "connections:\n  w:\n    adapter: postgres\n    sketch_all_columns: 1\n",
        )

        with pytest.raises(
            ConfigError,
            match="sketch_all_columns: expected true or false, got 1",
        ):
            load_project(tmp_path)
