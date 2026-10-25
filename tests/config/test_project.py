"""Project config loader tests - discovery, defaults cascade, validation."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from dbprint.config import (
    ConfigError,
    ConnectionConfig,
    DiffConfig,
    ProjectConfig,
    StatisticsProfile,
    load_project,
    load_project_at,
)
from tests._scripts import REPO_ROOT


EXAMPLE_DIR = REPO_ROOT / "docs/format/v1/examples/production"


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
        with pytest.raises(ConfigError, match=re.escape("no .dbprint.yaml found")):
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

        with pytest.raises(ConfigError, match=re.escape("no .dbprint.yaml at")):
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

        with pytest.raises(ConfigError, match=re.escape("no .dbprint.yaml at")):
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
        assert conn.tables.include == ("*",)
        assert conn.tables.exclude == ()
        assert conn.profile.freshness.max_age_days == 7
        assert conn.profile.statistics.enumeration_threshold == 50
        assert conn.profile.statistics.percentiles == (1, 25, 50, 75, 99)

    def test_defaults_cascade_into_connection(self, tmp_path: Path) -> None:
        _write_config(
            tmp_path,
            """
defaults:
  profile:
    freshness:
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
        assert conn.profile.freshness.max_age_days == 30
        assert conn.profile.statistics.top_n_values == 99

    def test_per_connection_overrides_defaults(self, tmp_path: Path) -> None:
        _write_config(
            tmp_path,
            """
defaults:
  profile:
    freshness:
      max_age_days: 30
    statistics:
      top_n_values: 99
connections:
  primary:
    adapter: postgres
    profile:
      freshness:
        max_age_days: 1
      statistics:
        top_n_values: 10
""",
        )
        cfg = load_project(tmp_path)
        conn = cfg.connections["primary"]
        assert conn.profile.freshness.max_age_days == 1
        assert conn.profile.statistics.top_n_values == 10

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
        assert thresholds["cardinality_ratio"] == 0.5  # noqa: RUF069 - the expected value is an exact literal
        assert thresholds["percentile_pct"] == 0.99  # noqa: RUF069 - the expected value is an exact literal
        # Unspecified key falls back to the built-in default.
        assert thresholds["default"] == 0.01  # noqa: RUF069 - the expected value is an exact literal

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
    tables:
      include:
      - Fixture.PUBLIC.*
""",
        )
        cfg = load_project(tmp_path)
        assert cfg.connections["primary"].tables.include == ("fixture.public.*",)

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

        with pytest.raises(
            ConfigError,
            match=r"connections\.primary\.adapter: expected one of 'postgres'",
        ):
            load_project(tmp_path)

    def test_no_connections_rejected(self, tmp_path: Path) -> None:
        _write_config(tmp_path, "connections: {}\n")

        with pytest.raises(ConfigError, match="at least one connection must be defined"):
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
    profile:
      statistics:
        percentiles:
        - 0
        - 50
        - 100
""",
        )

        with pytest.raises(
            ConfigError,
            match=r"percentiles\[0\]: expected an integer from 1 to 99, got 0",
        ):
            load_project(tmp_path)

    def test_fractional_percentile_rejected(self, tmp_path: Path) -> None:
        _write_config(
            tmp_path,
            """
connections:
  primary:
    adapter: postgres
    profile:
      statistics:
        percentiles:
        - 0.5
        - 25
        - 50
""",
        )

        with pytest.raises(
            ConfigError,
            match=r"percentiles\[0\]: expected an integer from 1 to 99, got 0\.5",
        ):
            load_project(tmp_path)


class TestStatisticsIntegersAreLocated:
    """The same malformed value must not report three ways depending on its block."""

    @pytest.mark.parametrize(
        "value",
        ['"twenty"', "true", "20.5", "[20]"],
        ids=["string", "boolean", "float", "list"],
    )
    def test_a_bad_connection_value_names_file_connection_and_key(
        self,
        tmp_path: Path,
        value: str,
    ) -> None:
        _write_config(
            tmp_path,
            f"""\
connections:
  w:
    adapter: postgres
    profile:
      statistics:
        top_n_values: {value}
""",
        )

        with pytest.raises(ConfigError) as exc:
            load_project(tmp_path)

        message = str(exc.value)
        assert ".dbprint.yaml" in message
        assert "connections.w." in message
        assert "top_n_values" in message

    def test_a_bad_defaults_value_names_the_defaults_block(self, tmp_path: Path) -> None:
        """The merge happens after validation, so the message names where the value was written."""

        _write_config(
            tmp_path,
            """\
defaults:
  profile:
    statistics:
      top_n_values: twenty
connections:
  w:
    adapter: postgres
""",
        )

        with pytest.raises(ConfigError, match=r"defaults\.profile\.statistics\.top_n_values"):
            load_project(tmp_path)

    def test_a_non_mapping_statistics_block_is_located(self, tmp_path: Path) -> None:
        _write_config(
            tmp_path,
            """\
connections:
  w:
    adapter: postgres
    profile:
      statistics: twenty
""",
        )

        with pytest.raises(ConfigError, match=r"statistics: expected a mapping"):
            load_project(tmp_path)

    def test_the_defaults_cascade_still_resolves(self, tmp_path: Path) -> None:
        _write_config(
            tmp_path,
            """\
defaults:
  profile:
    statistics:
      top_n_values: 99
      enumeration_threshold: 10
connections:
  w:
    adapter: postgres
    profile:
      statistics:
        top_n_values: 5
""",
        )

        stats = load_project(tmp_path).connections["w"].profile.statistics
        assert stats.top_n_values == 5
        assert stats.enumeration_threshold == 10


class TestFreshnessThresholdIsBounded:
    """A negative threshold is refused wherever it is written; zero is legal."""

    @pytest.mark.parametrize(
        "body",
        [
            """\
connections:
  w:
    adapter: postgres
    profile:
      freshness:
        max_age_days: -7
""",
            """\
defaults:
  profile:
    freshness:
      max_age_days: -7
connections:
  w:
    adapter: postgres
""",
            (
                """\
connections:
  w:
    adapter: postgres
    rules:
    - match:
        tables:
        - fixture.*
      profile:
        freshness:
          max_age_days: -1
"""
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
            """\
defaults:
  profile:
    freshness:
      max_age_days: -7
connections:
  w:
    adapter: postgres
""",
        )

        with pytest.raises(ConfigError) as exc:
            load_project(tmp_path)

        message = str(exc.value)
        assert ".dbprint.yaml" in message
        assert "defaults.profile.freshness.max_age_days" in message
        assert "-7" in message

    def test_a_rule_level_error_names_the_rule(self, tmp_path: Path) -> None:
        _write_config(
            tmp_path,
            """\
connections:
  w:
    adapter: postgres
    rules:
    - match:
        tables:
        - fixture.*
      profile:
        freshness:
          max_age_days: -1
""",
        )

        with pytest.raises(ConfigError, match=r"rules\[0\]\.profile\.freshness\.max_age_days"):
            load_project(tmp_path)

    def test_a_negative_in_defaults_is_refused_even_when_overridden(self, tmp_path: Path) -> None:
        """The block holding the value is where it is wrong, whether or not it is read."""

        _write_config(
            tmp_path,
            """\
defaults:
  profile:
    freshness:
      max_age_days: -7
connections:
  w:
    adapter: postgres
    profile:
      freshness:
        max_age_days: 7
""",
        )

        with pytest.raises(
            ConfigError,
            match=r"defaults\.profile\.freshness\.max_age_days: expected an integer of at least 0",
        ):
            load_project(tmp_path)

    @pytest.mark.parametrize(
        "value",
        ['"seven"', "true", "7.5", "[7]"],
        ids=["string", "boolean", "float", "list"],
    )
    def test_a_non_integer_threshold_is_refused(self, tmp_path: Path, value: str) -> None:
        _write_config(
            tmp_path,
            f"""\
connections:
  w:
    adapter: postgres
    profile:
      freshness:
        max_age_days: {value}
""",
        )

        with pytest.raises(ConfigError, match="expected an integer of at least 0"):
            load_project(tmp_path)

    def test_zero_is_legal_at_every_level(self, tmp_path: Path) -> None:
        """Zero says re-extract on every run, which the skip comparison already produces."""

        _write_config(
            tmp_path,
            """\
defaults:
  profile:
    freshness:
      max_age_days: 0
connections:
  w:
    adapter: postgres
    rules:
    - match:
        tables:
        - fixture.*
      profile:
        freshness:
          max_age_days: 0
""",
        )
        conn = load_project(tmp_path).connections["w"]

        assert conn.profile.freshness.max_age_days == 0
        assert conn.settings_for("fixture.curation_event", "table").max_age_days == 0

    def test_a_positive_and_an_absent_threshold_are_unchanged(self, tmp_path: Path) -> None:
        _write_config(
            tmp_path,
            """\
connections:
  w:
    adapter: postgres
    profile:
      freshness:
        max_age_days: 30
  x:
    adapter: postgres
""",
        )
        connections = load_project(tmp_path).connections

        assert connections["w"].profile.freshness.max_age_days == 30
        assert connections["x"].profile.freshness.max_age_days == 7


class TestReferenceExample:
    """The reference .dbprint.yaml in the format example must load cleanly."""

    def test_loads_reference_config(self) -> None:
        cfg = load_project(EXAMPLE_DIR)
        assert isinstance(cfg, ProjectConfig)
        assert "production" in cfg.connections
        conn = cfg.connections["production"]
        assert conn.adapter == "postgres"
        assert conn.auto is True
        assert conn.tables.include == ("arboretum.seedbank.*", "arboretum.fixture.*")
        assert conn.tables.exclude == ("arboretum.seedbank.audit_*",)
        assert conn.profile.freshness.max_age_days == 1
        assert isinstance(conn.profile.statistics, StatisticsProfile)
        assert conn.profile.statistics.top_n_values == 30
        assert conn.profile.statistics.enumeration_threshold == 50
        assert isinstance(conn.diff, DiffConfig)


class TestRules:
    """Per-table profiling rules: a matcher plus the settings it overrides."""

    def test_absent_by_default(self, tmp_path: Path) -> None:
        root = _write_config(
            tmp_path,
            "connections:\n  w:\n    adapter: postgres\n",
        )
        conn = load_project(root).connections["w"]
        settings = conn.settings_for("any.table", "table")

        assert conn.rules == ()
        assert settings.sample is None
        assert settings.filter is None
        assert settings.max_age_days == conn.profile.freshness.max_age_days
        assert settings.statistics == conn.profile.adapter_config()
        assert settings.matched_rules == ()

    def test_defaults_rules_apply_to_every_connection(self, tmp_path: Path) -> None:
        root = _write_config(
            tmp_path,
            """\
defaults:
  rules:
  - profile:
      read:
        tables:
          mode: sample
          fraction: 0.01
connections:
  w:
    adapter: postgres
""",
        )

        assert load_project(root).connections["w"].settings_for("a.b", "table").sample == 0.01  # noqa: RUF069 - the expected value is an exact literal

    def test_a_connection_rule_overrides_a_defaults_rule(self, tmp_path: Path) -> None:
        """Defaults rules are walked first, so a connection rule always wins."""

        root = _write_config(
            tmp_path,
            """\
defaults:
  rules:
  - profile:
      read:
        tables:
          mode: sample
          fraction: 0.01
connections:
  w:
    adapter: postgres
    rules:
    - profile:
        read:
          tables:
            mode: sample
            fraction: 0.5
""",
        )

        assert load_project(root).connections["w"].settings_for("a.b", "table").sample == 0.5  # noqa: RUF069 - the expected value is an exact literal

    def test_a_rule_applies_only_to_the_tables_it_matches(self, tmp_path: Path) -> None:
        root = _write_config(
            tmp_path,
            """\
connections:
  w:
    adapter: postgres
    rules:
    - match:
        tables:
        - fixture.*
      profile:
        read:
          tables:
            mode: sample
            fraction: 0.25
""",
        )
        conn = load_project(root).connections["w"]

        assert conn.settings_for("fixture.curation_event", "table").sample == 0.25  # noqa: RUF069 - the expected value is an exact literal
        assert conn.settings_for("public.curator", "table").sample is None

    def test_a_rule_exclude_removes_tables_from_its_own_match(self, tmp_path: Path) -> None:
        root = _write_config(
            tmp_path,
            """\
connections:
  w:
    adapter: postgres
    rules:
    - match:
        tables:
        - fixture.*
        exclude:
        - fixture.curation_event_v2
      profile:
        read:
          tables:
            mode: sample
            fraction: 0.25
""",
        )
        conn = load_project(root).connections["w"]

        assert conn.settings_for("fixture.curation_event", "table").sample == 0.25  # noqa: RUF069 - the expected value is an exact literal
        assert conn.settings_for("fixture.curation_event_v2", "table").sample is None

    def test_later_matching_rules_win_key_by_key(self, tmp_path: Path) -> None:
        """Declaration order decides, and an earlier rule's other keys survive."""

        root = _write_config(
            tmp_path,
            """\
connections:
  w:
    adapter: postgres
    rules:
    - match:
        tables:
        - '*'
      profile:
        read:
          tables:
            mode: filter
            filter: everything
        freshness:
          max_age_days: 30
    - match:
        tables:
        - fixture.curation_event
      profile:
        read:
          tables:
            mode: filter
            filter: narrow
""",
        )
        settings = (
            load_project(root).connections["w"].settings_for("fixture.curation_event", "table")
        )

        assert settings.filter == "narrow"
        assert settings.max_age_days == 30

    def test_matched_rules_names_every_matching_rule_in_declaration_order(
        self,
        tmp_path: Path,
    ) -> None:
        root = _write_config(
            tmp_path,
            """\
connections:
  w:
    adapter: postgres
    rules:
    - match:
        tables:
        - '*'
      profile:
        read:
          tables:
            mode: filter
            filter: everything
    - match:
        tables:
        - fixture.curation_event
      profile:
        read:
          tables:
            mode: filter
            filter: narrow
""",
        )
        settings = (
            load_project(root).connections["w"].settings_for("fixture.curation_event", "table")
        )

        assert settings.matched_rules == (
            "connections.w.rules[0]",
            "connections.w.rules[1]",
        )

    def test_matched_rules_excludes_a_rule_that_does_not_match_the_table(
        self,
        tmp_path: Path,
    ) -> None:
        root = _write_config(
            tmp_path,
            """\
connections:
  w:
    adapter: postgres
    rules:
    - match:
        tables:
        - fixture.*
      profile:
        read:
          tables:
            mode: filter
            filter: narrow
""",
        )
        conn = load_project(root).connections["w"]

        assert conn.settings_for("fixture.curation_event", "table").matched_rules == (
            "connections.w.rules[0]",
        )
        assert conn.settings_for("public.curator", "table").matched_rules == ()

    def test_declaration_order_decides_not_specificity(self, tmp_path: Path) -> None:
        """A broad rule declared last overrides a narrow one declared first."""

        root = _write_config(
            tmp_path,
            """\
connections:
  w:
    adapter: postgres
    rules:
    - match:
        tables:
        - fixture.curation_event
      profile:
        read:
          tables:
            mode: filter
            filter: narrow
    - match:
        tables:
        - '*'
      profile:
        read:
          tables:
            mode: filter
            filter: everything
""",
        )

        assert (
            load_project(root)
            .connections["w"]
            .settings_for("fixture.curation_event", "table")
            .filter
            == "everything"
        )

    def test_statistics_merge_key_by_key_across_rules(self, tmp_path: Path) -> None:
        root = _write_config(
            tmp_path,
            """\
connections:
  w:
    adapter: postgres
    rules:
    - match:
        tables:
        - '*'
      profile:
        statistics:
          top_n_values: 5
    - match:
        tables:
        - fixture.*
      profile:
        statistics:
          percentiles:
          - 50
""",
        )
        settings = (
            load_project(root).connections["w"].settings_for("fixture.curation_event", "table")
        )

        assert settings.statistics.top_n_values == 5
        assert settings.statistics.percentiles == (50,)
        assert settings.statistics.enumeration_threshold == 50

    def test_max_age_days_resolves_per_table(self, tmp_path: Path) -> None:
        root = _write_config(
            tmp_path,
            """\
connections:
  w:
    adapter: postgres
    profile:
      freshness:
        max_age_days: 7
    rules:
    - match:
        tables:
        - fixture.archived_*
      profile:
        freshness:
          max_age_days: 1
""",
        )
        conn = load_project(root).connections["w"]

        assert conn.settings_for("fixture.archived_curator", "table").max_age_days == 1
        assert conn.settings_for("fixture.curation_event", "table").max_age_days == 7

    def test_rule_selectors_are_lowercased_like_include_and_exclude(self, tmp_path: Path) -> None:
        root = _write_config(
            tmp_path,
            """\
connections:
  w:
    adapter: postgres
    rules:
    - match:
        tables:
        - Fixture.Curation_Event
      profile:
        read:
          tables:
            mode: filter
            filter: predicate
""",
        )

        assert (
            load_project(root)
            .connections["w"]
            .settings_for("fixture.curation_event", "table")
            .filter
        )

    @pytest.mark.parametrize("bad", ["0", "1.5", "-0.2", "'half'", "true"])
    def test_sample_outside_the_unit_interval_is_rejected(self, tmp_path: Path, bad: str) -> None:
        root = _write_config(
            tmp_path,
            f"""\
connections:
  w:
    adapter: postgres
    rules:
    - profile:
        read:
          tables:
            mode: sample
            fraction: {bad}
""",
        )

        with pytest.raises(
            ConfigError,
            match=r"rules\[0\]\.profile\.read\.tables\.fraction: expected a number in \(0, 1\]",
        ):
            load_project(root)

    def test_sample_of_one_is_accepted(self, tmp_path: Path) -> None:
        """The interval is (0, 1]: a whole-table sample is degenerate, not invalid."""

        root = _write_config(
            tmp_path,
            """\
connections:
  w:
    adapter: postgres
    rules:
    - profile:
        read:
          tables:
            mode: sample
            fraction: 1
""",
        )

        assert load_project(root).connections["w"].settings_for("a.b", "table").sample == 1.0  # noqa: RUF069 - the expected value is an exact literal

    @pytest.mark.parametrize("bad", ["''", "null", "3"], ids=["empty", "null", "number"])
    def test_filter_must_be_a_non_empty_string(self, tmp_path: Path, bad: str) -> None:
        root = _write_config(
            tmp_path,
            f"""\
connections:
  w:
    adapter: postgres
    rules:
    - profile:
        read:
          filter: {bad}
""",
        )

        with pytest.raises(ConfigError, match=r"rules\[0\]"):
            load_project(root)

    def test_rules_must_be_a_list(self, tmp_path: Path) -> None:
        root = _write_config(
            tmp_path,
            """\
connections:
  w:
    adapter: postgres
    rules:
      sample: 0.5
""",
        )

        with pytest.raises(ConfigError, match=r"connections\.w\.rules: expected a list"):
            load_project(root)

    def test_a_rule_that_changes_nothing_is_rejected(self, tmp_path: Path) -> None:
        """Otherwise a mis-nested key loads clean and silently does nothing."""

        root = _write_config(
            tmp_path,
            """\
connections:
  w:
    adapter: postgres
    rules:
    - match:
        tables:
        - a.*
      profile: {}
""",
        )

        with pytest.raises(ConfigError, match="would do nothing"):
            load_project(root)

    def test_a_rule_that_matches_nothing_is_rejected(self, tmp_path: Path) -> None:
        """The mirror of the empty rule - it sets something but can never fire."""

        root = _write_config(
            tmp_path,
            """\
connections:
  w:
    adapter: postgres
    rules:
    - match:
        tables: []
      profile:
        read:
          tables:
            mode: sample
            fraction: 0.5
""",
        )

        with pytest.raises(ConfigError, match=r"rules\[0\]\.match\.tables: is an empty list"):
            load_project(root)

    def test_an_omitted_include_matches_every_table(self, tmp_path: Path) -> None:
        root = _write_config(
            tmp_path,
            """\
connections:
  w:
    adapter: postgres
    rules:
    - profile:
        read:
          tables:
            mode: sample
            fraction: 0.5
""",
        )

        rule = load_project(root).connections["w"].rules[0]
        assert rule.match.tables == ("*",)
        assert rule.matches("anything.at.all")

    def test_an_explicit_wildcard_include_is_accepted(self, tmp_path: Path) -> None:
        root = _write_config(
            tmp_path,
            """\
connections:
  w:
    adapter: postgres
    rules:
    - match:
        tables:
        - '*'
      profile:
        read:
          tables:
            mode: sample
            fraction: 0.5
""",
        )

        assert load_project(root).connections["w"].rules[0].match.tables == ("*",)

    def test_an_empty_exclude_removes_nothing(self, tmp_path: Path) -> None:
        root = _write_config(
            tmp_path,
            """\
connections:
  w:
    adapter: postgres
    rules:
    - match:
        tables:
        - a.*
        exclude: []
      profile:
        read:
          tables:
            mode: sample
            fraction: 0.5
""",
        )

        rule = load_project(root).connections["w"].rules[0]
        assert rule.match.exclude == ()
        assert rule.matches("a.b")

    @pytest.mark.parametrize(
        ("key", "value", "answer"),
        [
            ("sample", "0.5", "unknown key; valid keys here:"),
            ("filter", '"x > 1"', "unknown key; did you mean 'profile.read.tables.filter'?"),
            (
                "min_rows",
                "1000",
                "unknown key; it belongs to a rule: did you mean 'rules[].match.min_rows'?",
            ),
        ],
    )
    def test_a_rule_key_hoisted_onto_the_connection_is_rejected(
        self,
        tmp_path: Path,
        key: str,
        value: str,
        answer: str,
    ) -> None:
        root = _write_config(
            tmp_path,
            f"connections:\n  w:\n    adapter: postgres\n    {key}: {value}\n",
        )

        with pytest.raises(ConfigError, match=re.escape(f"connections.w.{key}: {answer}")):
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

    def test_connection_level_statistics_and_max_age_are_still_read(self, tmp_path: Path) -> None:
        """Both legitimately live at this level and must not join the deny list."""

        root = _write_config(
            tmp_path,
            """\
connections:
  w:
    adapter: postgres
    profile:
      freshness:
        max_age_days: 3
      statistics:
        top_n_values: 5
""",
        )

        conn = load_project(root).connections["w"]
        assert conn.profile.freshness.max_age_days == 3
        assert conn.profile.statistics.top_n_values == 5


class TestSizeConditions:
    """`min_rows` is a third matcher field, so it gates every setting its rule carries."""

    def test_a_rule_applies_only_to_tables_over_its_bar(self, tmp_path: Path) -> None:
        conn = _size_rule_config(tmp_path)

        assert conn.settings_for("fixture.curation_event", "table", 800_000_000).sample == 0.01  # noqa: RUF069 - the expected value is an exact literal
        assert conn.settings_for("fixture.curation_event", "table", 1000).sample is None

    def test_the_bar_is_inclusive(self, tmp_path: Path) -> None:
        conn = _size_rule_config(tmp_path)

        assert conn.settings_for("fixture.curation_event", "table", 500_000_000).sample == 0.01  # noqa: RUF069 - the expected value is an exact literal

    def test_the_name_matcher_still_applies(self, tmp_path: Path) -> None:
        """Both conditions hold or the rule does not govern the table."""

        conn = _size_rule_config(tmp_path)

        assert conn.settings_for("other.curation_event", "table", 900_000_000).sample is None

    def test_an_unavailable_estimate_leaves_the_rule_unapplied(self, tmp_path: Path) -> None:
        conn = _size_rule_config(tmp_path)

        assert conn.settings_for("fixture.curation_event", "table").sample is None

    def test_a_known_empty_table_fails_the_bar_rather_than_going_unjudged(
        self,
        tmp_path: Path,
    ) -> None:
        """`0` is a size the catalog knows; it is not the absent estimate."""

        conn = _size_rule_config(tmp_path)

        assert conn.settings_for("fixture.curation_event", "table", 0).sample is None

    def test_the_bar_gates_every_setting_the_rule_carries(self, tmp_path: Path) -> None:
        root = _write_config(
            tmp_path,
            """\
connections:
  w:
    adapter: postgres
    profile:
      freshness:
        max_age_days: 7
    rules:
    - match:
        tables:
        - fixture.*
        min_rows: 1000
      profile:
        freshness:
          max_age_days: 30
""",
        )
        conn = load_project(root).connections["w"]

        assert conn.settings_for("fixture.curation_event", "table", 5000).max_age_days == 30
        assert conn.settings_for("fixture.curation_event", "table", 10).max_age_days == 7

    def test_a_rule_carrying_only_a_size_condition_is_rejected(self, tmp_path: Path) -> None:
        """A matcher is not a setting: it selects tables and overrides nothing on them."""

        root = _write_config(
            tmp_path,
            """\
connections:
  w:
    adapter: postgres
    rules:
    - match:
        min_rows: 1000
      profile: {}
""",
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
            f"""\
connections:
  w:
    adapter: postgres
    rules:
    - match:
        min_rows: {bad}
      profile:
        read:
          sample: 0.5
""",
        )

        with pytest.raises(
            ConfigError,
            match=r"rules\[0\]\.match\.min_rows: expected an integer of at least 1",
        ):
            load_project(root)

    def test_the_hoisting_error_suggests_a_rule_that_would_load(self, tmp_path: Path) -> None:
        """A size condition only selects tables, so it is pointed at a rule's `match`."""

        root = _write_config(
            tmp_path,
            "connections:\n  w:\n    adapter: postgres\n    min_rows: 1000\n",
        )

        with pytest.raises(ConfigError) as excinfo:
            load_project(root)

        assert "did you mean 'rules[].match.min_rows'?" in str(excinfo.value)

    def test_a_cascade_without_a_size_condition_reports_it_reads_no_row_counts(
        self,
        tmp_path: Path,
    ) -> None:
        root = _write_config(
            tmp_path,
            """\
connections:
  w:
    adapter: postgres
    rules:
    - match:
        tables:
        - fixture.*
      profile:
        read:
          tables:
            mode: sample
            fraction: 0.5
""",
        )

        assert not load_project(root).connections["w"].reads_row_counts

    def test_a_cascade_with_a_size_condition_reports_it_reads_row_counts(
        self,
        tmp_path: Path,
    ) -> None:
        assert _size_rule_config(tmp_path).reads_row_counts

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
            """\
connections:
  w:
    adapter: postgres
    rules:
    - match:
        tables:
        - fixture.*
        min_rows: 1000
      profile:
        read:
          tables:
            mode: sample
            fraction: 0.01
    - match:
        tables:
        - other.*
      profile:
        read:
          tables:
            mode: sample
            fraction: 0.5
""",
        )
        conn = load_project(root).connections["w"]

        assert not conn.size_conditions_name("other.curation_event")


class TestMinRowsConditions:
    """`min_rows_conditions_name` narrows `size_conditions_name` to the question offline
    freshness fallback actually asks - a ceiling cannot affect `max_age_days` at all.
    """

    def test_a_connection_level_ceiling_alone_names_nothing(self, tmp_path: Path) -> None:
        root = _write_config(
            tmp_path,
            """\
connections:
  w:
    adapter: postgres
    profile:
      read:
        tables:
          mode: sample
          max_rows: 1000000000
""",
        )
        conn = load_project(root).connections["w"]

        assert not conn.min_rows_conditions_name("anything.at_all")

    def test_a_rules_ceiling_alone_names_nothing(self, tmp_path: Path) -> None:
        root = _write_config(
            tmp_path,
            """\
connections:
  w:
    adapter: postgres
    rules:
    - match:
        tables:
        - fixture.*
      profile:
        read:
          tables:
            mode: sample
            max_rows: 1000000000
        freshness:
          max_age_days: 30
""",
        )
        conn = load_project(root).connections["w"]

        assert not conn.min_rows_conditions_name("fixture.curation_event")

    def test_a_rules_min_rows_names_it(self, tmp_path: Path) -> None:
        conn = _size_rule_config(tmp_path)

        assert conn.min_rows_conditions_name("fixture.curation_event")
        assert not conn.min_rows_conditions_name("other.curation_event")


def _size_rule_config(tmp_path: Path) -> ConnectionConfig:
    root = _write_config(
        tmp_path,
        """\
connections:
  w:
    adapter: postgres
    rules:
    - match:
        tables:
        - fixture.*
        min_rows: 500000000
      profile:
        read:
          tables:
            mode: sample
            fraction: 0.01
""",
    )

    return load_project(root).connections["w"]


class TestMaxRowsScanned:
    """A row-count ceiling: the operator states rows, `settings_for` derives the fraction."""

    def test_a_table_over_the_ceiling_is_narrowed(self, tmp_path: Path) -> None:
        root = _write_config(
            tmp_path,
            """\
connections:
  w:
    adapter: postgres
    profile:
      read:
        tables:
          mode: sample
          max_rows: 1000000000
""",
        )
        settings = load_project(root).connections["w"].settings_for("a.b", "table", 10_000_000_000)

        assert settings.sample is not None
        assert settings.sample * 10_000_000_000 <= 1_000_000_000

    def test_a_table_under_the_ceiling_is_read_whole(self, tmp_path: Path) -> None:
        root = _write_config(
            tmp_path,
            """\
connections:
  w:
    adapter: postgres
    profile:
      read:
        tables:
          mode: sample
          max_rows: 1000000000
""",
        )

        settings = load_project(root).connections["w"].settings_for("a.b", "table", 200_000_000)

        assert settings.sample is None
        assert settings.max_rows_scanned == 1_000_000_000

    def test_an_estimate_at_the_ceiling_is_read_whole(self, tmp_path: Path) -> None:
        """At or above the cap is "no narrowing", not a fraction of 1.0 (SPEC 2.2.8)."""

        root = _write_config(
            tmp_path,
            """\
connections:
  w:
    adapter: postgres
    profile:
      read:
        tables:
          mode: sample
          max_rows: 1000000000
""",
        )

        assert (
            load_project(root).connections["w"].settings_for("a.b", "table", 1_000_000_000).sample
            is None
        )

    def test_small_drift_leaves_the_fraction_unchanged(self, tmp_path: Path) -> None:
        root = _write_config(
            tmp_path,
            """\
connections:
  w:
    adapter: postgres
    profile:
      read:
        tables:
          mode: sample
          max_rows: 1000000000
""",
        )
        conn = load_project(root).connections["w"]

        before = conn.settings_for("a.b", "table", 10_000_000_000).sample
        after = conn.settings_for("a.b", "table", 10_300_000_000).sample

        assert before == after

    def test_a_genuine_size_move_steps_the_fraction(self, tmp_path: Path) -> None:
        root = _write_config(
            tmp_path,
            """\
connections:
  w:
    adapter: postgres
    profile:
      read:
        tables:
          mode: sample
          max_rows: 1000000000
""",
        )
        conn = load_project(root).connections["w"]

        before = conn.settings_for("a.b", "table", 10_000_000_000).sample
        after = conn.settings_for("a.b", "table", 20_000_000_000).sample

        assert before != after

    def test_a_missing_estimate_leaves_the_table_unnarrowed(self, tmp_path: Path) -> None:
        root = _write_config(
            tmp_path,
            """\
connections:
  w:
    adapter: postgres
    profile:
      read:
        tables:
          mode: sample
          max_rows: 1000000000
""",
        )

        assert load_project(root).connections["w"].settings_for("a.b", "table").sample is None

    def test_valid_at_rule_connection_and_defaults_level(self, tmp_path: Path) -> None:
        root = _write_config(
            tmp_path,
            """\
defaults:
  profile:
    read:
      tables:
        mode: sample
        max_rows: 2000000000
connections:
  w:
    adapter: postgres
    rules:
    - match:
        tables:
        - fixture.*
      profile:
        read:
          tables:
            mode: sample
            max_rows: 500000000
""",
        )
        conn = load_project(root).connections["w"]

        assert (
            conn.settings_for("fixture.curation_event", "table", 1_000_000_000).sample is not None
        )
        assert conn.settings_for("other.curation_event", "table", 5_000_000_000).sample is not None

    def test_a_later_sample_beats_an_earlier_ceiling(self, tmp_path: Path) -> None:
        root = _write_config(
            tmp_path,
            """\
connections:
  w:
    adapter: postgres
    profile:
      read:
        tables:
          mode: sample
          max_rows: 1000000000
    rules:
    - match:
        tables:
        - '*'
      profile:
        read:
          tables:
            mode: sample
            fraction: 0.5
""",
        )

        settings = load_project(root).connections["w"].settings_for("a.b", "table", 10_000_000_000)

        assert settings.sample == 0.5  # noqa: RUF069 - the expected value is an exact literal
        assert settings.max_rows_scanned is None

    def test_a_later_ceiling_beats_an_earlier_explicit_sample(self, tmp_path: Path) -> None:
        """Declaration order alone decides - neither directive has fixed priority."""

        root = _write_config(
            tmp_path,
            """\
connections:
  w:
    adapter: postgres
    rules:
    - match:
        tables:
        - '*'
      profile:
        read:
          tables:
            mode: sample
            fraction: 0.5
    - match:
        tables:
        - '*'
      profile:
        read:
          tables:
            mode: sample
            max_rows: 1000000000
""",
        )
        settings = load_project(root).connections["w"].settings_for("a.b", "table", 10_000_000_000)

        assert settings.sample is not None
        assert settings.sample != 0.5  # noqa: RUF069 - the expected value is an exact literal
        assert settings.sample * 10_000_000_000 <= 1_000_000_000

    def test_composed_with_min_rows_the_two_conditions_are_orthogonal(self, tmp_path: Path) -> None:
        root = _write_config(
            tmp_path,
            """\
connections:
  w:
    adapter: postgres
    rules:
    - match:
        tables:
        - fixture.*
        min_rows: 500000000
      profile:
        read:
          tables:
            mode: sample
            max_rows: 1000000000
""",
        )
        conn = load_project(root).connections["w"]

        assert (
            conn.settings_for("fixture.curation_event", "table", 10_000_000_000).sample is not None
        )
        assert conn.settings_for("fixture.curation_event", "table", 1000).sample is None

    def test_a_config_with_neither_ceiling_nor_min_rows_reads_no_row_counts(
        self,
        tmp_path: Path,
    ) -> None:
        root = _write_config(
            tmp_path,
            "connections:\n  w:\n    adapter: postgres\n",
        )

        assert not load_project(root).connections["w"].reads_row_counts

    def test_a_connection_level_ceiling_alone_reads_row_counts(self, tmp_path: Path) -> None:
        root = _write_config(
            tmp_path,
            """\
connections:
  w:
    adapter: postgres
    profile:
      read:
        tables:
          mode: sample
          max_rows: 1000000000
""",
        )

        assert load_project(root).connections["w"].reads_row_counts

    def test_a_connection_level_ceiling_names_every_table(self, tmp_path: Path) -> None:
        root = _write_config(
            tmp_path,
            """\
connections:
  w:
    adapter: postgres
    profile:
      read:
        tables:
          mode: sample
          max_rows: 1000000000
""",
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
            f"""\
connections:
  w:
    adapter: postgres
    profile:
      read:
        tables:
          mode: sample
          max_rows: {bad}
""",
        )

        with pytest.raises(
            ConfigError,
            match=r"connections\.w\.profile\.read\.tables\.max_rows: expected an integer of at least 1",
        ):
            load_project(root)

    def test_a_rule_ceiling_that_is_not_a_positive_integer_names_the_rule(
        self,
        tmp_path: Path,
    ) -> None:
        root = _write_config(
            tmp_path,
            """\
connections:
  w:
    adapter: postgres
    rules:
    - profile:
        read:
          tables:
            mode: sample
            max_rows: 0
""",
        )

        with pytest.raises(ConfigError, match=r"rules\[0\]\.profile\.read\.tables\.max_rows"):
            load_project(root)

    def test_a_rule_carrying_only_a_ceiling_is_accepted(self, tmp_path: Path) -> None:
        """Unlike `min_rows`, a ceiling is a setting on its own - it needs no partner key."""

        root = _write_config(
            tmp_path,
            """\
connections:
  w:
    adapter: postgres
    rules:
    - profile:
        read:
          tables:
            mode: sample
            max_rows: 1000000000
""",
        )

        rules = load_project(root).connections["w"].rules
        assert len(rules) == 1
        assert rules[0].profile["read"]["tables"]["max_rows"] == 1000000000

    def test_a_rule_carrying_nothing_at_all_is_still_rejected(self, tmp_path: Path) -> None:
        root = _write_config(
            tmp_path,
            """\
connections:
  w:
    adapter: postgres
    rules:
    - match:
        tables:
        - '*'
      profile: {}
""",
        )

        with pytest.raises(
            ConfigError,
            match=r"rules\[0\]\.profile: sets nothing, so it would do nothing",
        ):
            load_project(root)


class TestRuleErrorsNameTheRule:
    """Rule keys reuse the connection's coercion helpers, so errors must still name the rule."""

    def test_rule_include_names_the_rule(self, tmp_path: Path) -> None:
        root = _write_config(
            tmp_path,
            """\
connections:
  w:
    adapter: postgres
    rules:
    - match:
        tables:
        - a.*
      profile:
        read:
          tables:
            mode: sample
            fraction: 0.5
    - match:
        tables: a.b
      profile:
        read:
          tables:
            mode: sample
            fraction: 0.5
""",
        )

        with pytest.raises(
            ConfigError,
            match=r"rules\[1\]\.match\.tables: expected a list of strings",
        ):
            load_project(root)

    def test_rule_exclude_names_the_rule(self, tmp_path: Path) -> None:
        root = _write_config(
            tmp_path,
            """\
connections:
  w:
    adapter: postgres
    rules:
    - match:
        tables:
        - a.*
      profile:
        read:
          tables:
            mode: sample
            fraction: 0.5
    - match:
        exclude: a.b
      profile:
        read:
          tables:
            mode: sample
            fraction: 0.5
""",
        )

        with pytest.raises(
            ConfigError,
            match=r"rules\[1\]\.match\.exclude: expected a list of strings",
        ):
            load_project(root)

    def test_rule_percentiles_name_the_rule(self, tmp_path: Path) -> None:
        root = _write_config(
            tmp_path,
            """\
connections:
  w:
    adapter: postgres
    rules:
    - profile:
        statistics:
          percentiles:
          - 0.5
""",
        )

        with pytest.raises(ConfigError, match=r"rules\[0\]\.profile\.statistics\.percentiles\[0\]"):
            load_project(root)

    def test_a_defaults_rule_names_the_list_it_came_from(self, tmp_path: Path) -> None:
        root = _write_config(
            tmp_path,
            """\
defaults:
  rules:
  - match:
      tables: a.b
    profile:
      read:
        tables:
          mode: sample
          fraction: 0.5
connections:
  w:
    adapter: postgres
""",
        )

        with pytest.raises(ConfigError, match=r"defaults\.rules\[0\]\.match\.tables"):
            load_project(root)

    def test_a_connection_selector_names_only_itself(self, tmp_path: Path) -> None:
        root = _write_config(
            tmp_path,
            """\
connections:
  w:
    adapter: postgres
    tables:
      include: a.b
""",
        )

        with pytest.raises(ConfigError) as exc:
            load_project(root)

        message = str(exc.value)
        assert "connections.w.tables.include: expected a list of strings" in message
        assert "rules[" not in message

    def test_a_connection_percentile_names_only_itself(self, tmp_path: Path) -> None:
        root = _write_config(
            tmp_path,
            """\
connections:
  w:
    adapter: postgres
    profile:
      statistics:
        percentiles:
        - 0.5
""",
        )

        with pytest.raises(ConfigError) as exc:
            load_project(root)

        message = str(exc.value)
        assert "connections.w.profile.statistics.percentiles[0]: expected an integer" in message
        assert "rules[" not in message


class TestRedactTargetsAreCheckedAgainstTheirVocabularies:
    """A rule naming no target is a load error; one naming an unknown target covered nothing.

    Both select no column, but a misspelled target would load clean and write the literals.
    """

    def test_an_unknown_sensitivity_is_rejected(self, tmp_path: Path) -> None:
        root = _write_config(
            tmp_path,
            """\
connections:
  w:
    adapter: postgres
    profile:
      redact:
      - sensitivity:
        - personal_names
        with: drop
""",
        )

        with pytest.raises(ConfigError, match=r"redact\[0\]\.sensitivity"):
            load_project(root)

    def test_the_rejection_lists_the_accepted_values(self, tmp_path: Path) -> None:
        root = _write_config(
            tmp_path,
            """\
connections:
  w:
    adapter: postgres
    profile:
      redact:
      - sensitivity:
        - personal_names
        with: drop
""",
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
            """\
connections:
  w:
    adapter: postgres
    profile:
      redact:
      - looks_like:
        - e-mail
        with: hash
""",
        )

        with pytest.raises(ConfigError, match=r"redact\[0\]\.looks_like"):
            load_project(root)

    def test_a_rule_naming_ipv4_is_rejected(self, tmp_path: Path) -> None:
        """`ipv4` is not in the current vocabulary; a rule naming it is refused like any typo."""

        root = _write_config(
            tmp_path,
            """\
connections:
  w:
    adapter: postgres
    profile:
      redact:
      - looks_like:
        - ipv4
        with: mask
""",
        )

        with pytest.raises(ConfigError, match=r"redact\[0\]\.looks_like") as excinfo:
            load_project(root)

        assert "'ipv4'" in str(excinfo.value)

    def test_a_partly_valid_rule_is_not_partly_applied(self, tmp_path: Path) -> None:
        """One bad target refuses the whole rule; the good target alongside it does not save it."""

        root = _write_config(
            tmp_path,
            """\
connections:
  w:
    adapter: postgres
    profile:
      redact:
      - columns:
        - '*.curator.email'
        sensitivity:
        - typo
""",
        )

        with pytest.raises(ConfigError, match=r"redact\[0\]\.sensitivity"):
            load_project(root)

    def test_a_value_in_another_case_is_refused(self, tmp_path: Path) -> None:
        """The schema an editor validates against is case-sensitive, so the loader is too."""

        root = _write_config(
            tmp_path,
            """\
connections:
  w:
    adapter: postgres
    profile:
      redact:
      - sensitivity:
        - PERSONAL_NAME
        with: drop
""",
        )

        with pytest.raises(ConfigError, match=r"redact\[0\]\.sensitivity\[0\]: expected one of"):
            load_project(root)

    def test_columns_stays_an_open_vocabulary(self, tmp_path: Path) -> None:
        """A glob matching no table today may match one tomorrow, so it is not checked."""

        root = _write_config(
            tmp_path,
            """\
connections:
  w:
    adapter: postgres
    profile:
      redact:
      - columns:
        - '*.no_such_table.no_such_column'
        with: mask
""",
        )

        assert load_project(root).connections["w"].profile.redact[0].columns == (
            "*.no_such_table.no_such_column",
        )


class TestRedactCascadesFromDefaults:
    """`redact` is a list, so a connection's list replaces the `defaults` one whole."""

    def test_a_defaults_rule_reaches_every_connection(self, tmp_path: Path) -> None:
        root = _write_config(
            tmp_path,
            """\
defaults:
  profile:
    redact:
    - sensitivity:
      - personal_name
      with: drop
connections:
  w:
    adapter: postgres
  x:
    adapter: mysql
""",
        )
        connections = load_project(root).connections

        for name in ("w", "x"):
            assert connections[name].redaction_for("a.b.c", "personal_name", None) == "drop"

    def test_a_connection_list_replaces_the_defaults_list(self, tmp_path: Path) -> None:
        root = _write_config(
            tmp_path,
            """\
defaults:
  profile:
    redact:
    - sensitivity:
      - contact
      with: mask
connections:
  w:
    adapter: postgres
    profile:
      redact:
      - sensitivity:
        - contact
        with: hash
""",
        )
        conn = load_project(root).connections["w"]

        assert [r.with_ for r in conn.profile.redact] == ["hash"]
        assert conn.redaction_for("a.b.c", "contact", None) == "hash"

    def test_a_defaults_rule_left_out_of_a_connection_list_no_longer_applies(
        self,
        tmp_path: Path,
    ) -> None:

        root = _write_config(
            tmp_path,
            """\
defaults:
  profile:
    redact:
    - sensitivity:
      - contact
      with: mask
connections:
  w:
    adapter: postgres
    profile:
      redact:
      - columns:
        - a.b.other
        with: drop
""",
        )
        conn = load_project(root).connections["w"]

        assert conn.redaction_for("a.b.c", "contact", None) is None
        assert conn.redaction_for("a.b.other", None, None) == "drop"

    def test_a_defaults_rule_names_the_list_it_came_from(self, tmp_path: Path) -> None:
        root = _write_config(
            tmp_path,
            """\
defaults:
  profile:
    redact:
    - sensitivity:
      - typo
      with: drop
connections:
  w:
    adapter: postgres
""",
        )

        with pytest.raises(
            ConfigError,
            match=r"defaults\.profile\.redact\[0\]\.sensitivity\[0\]: expected one of",
        ):
            load_project(root)

    def test_a_connection_rule_names_the_connection(self, tmp_path: Path) -> None:
        root = _write_config(
            tmp_path,
            """\
connections:
  w:
    adapter: postgres
    profile:
      redact:
      - sensitivity:
        - typo
        with: drop
""",
        )

        with pytest.raises(
            ConfigError,
            match=r"connections\.w\.profile\.redact\[0\]\.sensitivity\[0\]: expected one of",
        ):
            load_project(root)

    def test_a_defaults_redact_that_is_not_a_list_is_rejected(self, tmp_path: Path) -> None:
        root = _write_config(
            tmp_path,
            """\
defaults:
  profile:
    redact:
      sensitivity:
      - contact
connections:
  w:
    adapter: postgres
""",
        )

        with pytest.raises(ConfigError, match=r"defaults\.profile\.redact: expected a list"):
            load_project(root)

    def test_no_redact_anywhere_leaves_the_connection_empty(self, tmp_path: Path) -> None:
        root = _write_config(tmp_path, "connections:\n  w:\n    adapter: postgres\n")

        assert load_project(root).connections["w"].profile.redact == ()


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
        assert "connections.w." in message
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

        with pytest.raises(ConfigError, match=r"expected a number in \[0, 1\]"):
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
        assert "stat_change_threshold.top_values_coverage: unknown key" in message
        assert "did you mean 'values_coverage'?" in message

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

        with pytest.raises(ConfigError, match=r"diff\.stat_change_threshold: expected a mapping"):
            load_project(tmp_path)

    def test_a_non_mapping_diff_block_is_located(self, tmp_path: Path) -> None:
        _write_config(
            tmp_path,
            "connections:\n  w:\n    adapter: postgres\n    diff: 0.02\n",
        )

        with pytest.raises(ConfigError) as refused:
            load_project(tmp_path)

        assert str(refused.value).endswith("connections.w.diff: expected a mapping, got 0.02")

    def test_a_non_mapping_defaults_diff_block_names_the_defaults(self, tmp_path: Path) -> None:
        _write_config(
            tmp_path,
            "defaults:\n  diff: 0.02\nconnections:\n  w:\n    adapter: postgres\n",
        )

        with pytest.raises(ConfigError) as refused:
            load_project(tmp_path)

        assert "defaults.diff: expected a mapping, got 0.02" in str(refused.value)

    def test_a_whole_number_is_a_well_formed_fraction(self, tmp_path: Path) -> None:
        """1 means "show every change", which is a threshold like any other."""

        _write_config(
            tmp_path,
            "connections:\n  w:\n    adapter: postgres\n    diff:\n"
            "      stat_change_threshold:\n        default: 1\n",
        )
        conn = load_project(tmp_path).connections["w"]

        assert conn.diff.stat_change_threshold["default"] == 1.0  # noqa: RUF069 - the expected value is an exact literal

    def test_a_valid_block_still_merges_over_the_spec_defaults(self, tmp_path: Path) -> None:
        _write_config(
            tmp_path,
            "connections:\n  w:\n    adapter: postgres\n    diff:\n"
            "      stat_change_threshold:\n        cardinality_ratio: 0.5\n",
        )
        thresholds = load_project(tmp_path).connections["w"].diff.stat_change_threshold

        assert thresholds["cardinality_ratio"] == 0.5  # noqa: RUF069 - the expected value is an exact literal
        assert thresholds["default"] == 0.01  # noqa: RUF069 - the expected value is an exact literal

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

        with pytest.raises(ConfigError, match=r"connections\.w\.diff: expected a mapping"):
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

        with pytest.raises(ConfigError, match=r"defaults\.diff: expected a mapping"):
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

        with pytest.raises(ConfigError, match=r"diff\.stat_change_threshold: expected a mapping"):
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
            match=r"defaults\.diff\.stat_change_threshold: expected a mapping",
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
            f"""\
connections:
  w:
    adapter: postgres
    profile:
      statistics: {value}
""",
        )

        with pytest.raises(ConfigError, match=r"statistics: expected a mapping"):
            load_project(tmp_path)

    @pytest.mark.parametrize(
        "value",
        ["0", '""', "[]", "false"],
        ids=["zero", "empty_string", "empty_list", "false"],
    )
    def test_a_falsy_defaults_statistics_block_is_refused(self, tmp_path: Path, value: str) -> None:
        _write_config(
            tmp_path,
            f"""\
defaults:
  profile:
    statistics: {value}
connections:
  w:
    adapter: postgres
""",
        )

        with pytest.raises(ConfigError, match=r"defaults\.profile\.statistics: expected a mapping"):
            load_project(tmp_path)

    @pytest.mark.parametrize(
        "value",
        ["0", '""', "[]", "false"],
        ids=["zero", "empty_string", "empty_list", "false"],
    )
    def test_a_falsy_rule_statistics_block_is_refused(self, tmp_path: Path, value: str) -> None:
        _write_config(
            tmp_path,
            f"""\
connections:
  w:
    adapter: postgres
    rules:
    - match:
        tables:
        - a.*
      profile:
        statistics: {value}
""",
        )

        with pytest.raises(ConfigError, match=r"statistics: expected a mapping"):
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

        with pytest.raises(ConfigError, match=r"connections\.w\.assertions: expected a mapping"):
            load_project(tmp_path)

    def test_an_empty_mapping_diff_loads_clean(self, tmp_path: Path) -> None:
        """`diff: {}` means the same as an absent block."""

        _write_config(tmp_path, "connections:\n  w:\n    adapter: postgres\n    diff: {}\n")
        diff = load_project(tmp_path).connections["w"].diff

        assert diff.stat_change_threshold == {
            "cardinality_ratio": 0.02,
            "percentile_pct": 0.05,
            "values_coverage": 0.05,
            "default": 0.01,
        }

    def test_a_null_diff_is_refused(self, tmp_path: Path) -> None:
        _write_config(tmp_path, "connections:\n  w:\n    adapter: postgres\n    diff:\n")

        with pytest.raises(
            ConfigError,
            match=r"connections\.w\.diff: expected a mapping, got None",
        ):
            load_project(tmp_path)


class TestFeatureObjects:
    """Every optional measurement is an object valid at every level (SPEC 2.2.21)."""

    def test_the_defaults_switch_everything_on(self, tmp_path: Path) -> None:
        _write_config(tmp_path, "connections:\n  w:\n    adapter: postgres\n")
        profile = load_project(tmp_path).connections["w"].profile

        assert profile.disabled() == ()
        assert (profile.null_patterns.top_n, profile.looks_like.sample_size) == (20, 1000)
        assert (profile.parts.max, profile.parts.max_depth) == (20, 3)
        assert profile.sketches.columns == "required"

    def test_a_rule_switches_one_measurement_off_for_one_table(self, tmp_path: Path) -> None:
        _write_config(
            tmp_path,
            """\
connections:
  w:
    adapter: postgres
    rules:
    - match: {tables: [public.wide]}
      profile: {null_patterns: {enabled: false}, relationships: {infer: false}}
""",
        )
        conn = load_project(tmp_path).connections["w"]

        assert conn.settings_for("public.wide", "table").profile.disabled() == (
            "null_patterns",
            "relationships.infer",
        )
        assert conn.settings_for("public.other", "table").profile.disabled() == ()

    def test_a_connection_overrides_defaults_key_by_key(self, tmp_path: Path) -> None:
        _write_config(
            tmp_path,
            """\
defaults:
  profile:
    sketches: {columns: all}
    parts: {max: 5}
connections:
  inherits:
    adapter: postgres
  overrides:
    adapter: postgres
    profile:
      sketches: {enabled: false}
      parts: {max_depth: 1}
""",
        )
        connections = load_project(tmp_path).connections
        inherits, overrides = connections["inherits"].profile, connections["overrides"].profile

        assert (inherits.sketches.columns, inherits.parts.max) == ("all", 5)
        assert (overrides.sketches.enabled, overrides.sketches.columns) == (False, "all")
        assert (overrides.parts.max, overrides.parts.max_depth) == (5, 1)

    def test_a_switched_off_feature_reaches_the_adapter_as_a_zero_bound(
        self,
        tmp_path: Path,
    ) -> None:
        _write_config(
            tmp_path,
            """\
connections:
  w:
    adapter: postgres
    profile: {parts: {enabled: false}, looks_like: {enabled: false}, null_patterns: {top_n: 7}}
""",
        )
        bounds = load_project(tmp_path).connections["w"].profile.adapter_config()

        assert (bounds.max_parts, bounds.looks_like_sample_size) == (0, 0)
        assert bounds.top_n_null_patterns == 7

    @pytest.mark.parametrize(
        ("block", "message"),
        [
            ("{parts: {max: 0}}", "parts.max: expected an integer of at least 1"),
            ("{sketches: {columns: some}}", "sketches.columns: expected one of"),
            (
                "{looks_like: {sample_size: 0}}",
                "looks_like.sample_size: expected an integer of at least 1",
            ),
            ("{timeline: {enabled: 1}}", "timeline.enabled: expected true or false, got 1"),
        ],
    )
    def test_a_bad_value_is_refused(self, tmp_path: Path, block: str, message: str) -> None:
        _write_config(
            tmp_path,
            f"connections:\n  w:\n    adapter: postgres\n    profile: {block}\n",
        )

        with pytest.raises(ConfigError, match=re.escape(message)):
            load_project(tmp_path)

    def test_a_rule_setting_redact_is_sent_to_the_connection(self, tmp_path: Path) -> None:
        _write_config(
            tmp_path,
            """\
connections:
  w:
    adapter: postgres
    rules:
    - match: {tables: [public.herbarium]}
      profile: {redact: [{columns: ["*.email"], with: drop}]}
""",
        )
        answer = (
            "connections.w.rules[0].profile.redact: unknown key; it is set per connection, "
            "never in a rule: 'connections.<name>.profile.redact'"
        )

        with pytest.raises(ConfigError, match=re.escape(answer)):
            load_project(tmp_path)

    def test_a_nested_rule_typo_is_not_sent_to_an_unrelated_connection_key(
        self,
        tmp_path: Path,
    ) -> None:
        _write_config(
            tmp_path,
            "connections:\n  w:\n    adapter: postgres\n    rules:\n"
            "    - {match: {include: [a.*]}, profile: {timeline: {enabled: false}}}\n",
        )

        with pytest.raises(
            ConfigError,
            match=re.escape("rules[0].match.include: unknown key; did you mean 'exclude'?"),
        ):
            load_project(tmp_path)

    def test_a_connection_redact_reaches_one_table_by_a_column_glob(self, tmp_path: Path) -> None:
        _write_config(
            tmp_path,
            """\
connections:
  w:
    adapter: postgres
    profile: {redact: [{columns: ["public.herbarium.*"], with: drop}]}
""",
        )
        conn = load_project(tmp_path).connections["w"]

        assert conn.redaction_for("public.herbarium.email", None, None) == "drop"
        assert conn.redaction_for("public.other.email", None, None) is None


class TestDbtManifest:
    def _load(self, tmp_path: Path, body: str) -> Any:
        _write_config(tmp_path, body)

        return load_project(tmp_path).connections["primary"]

    def test_a_relative_path_resolves_against_the_config_directory(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.chdir(tmp_path.parent)
        conn = self._load(
            tmp_path,
            "connections:\n  primary:\n    adapter: duckdb\n    dbt_manifest: target/manifest.json\n",
        )

        assert conn.dbt_manifest == (tmp_path / "target" / "manifest.json").resolve()

    def test_a_home_path_expands(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        conn = self._load(
            tmp_path,
            "connections:\n  primary:\n    adapter: duckdb\n    dbt_manifest: ~/.dbt/manifest.json\n",
        )

        assert conn.dbt_manifest == tmp_path / "home" / ".dbt" / "manifest.json"

    def test_absent_is_none(self, tmp_path: Path) -> None:
        conn = self._load(tmp_path, "connections:\n  primary:\n    adapter: duckdb\n")

        assert conn.dbt_manifest is None

    def test_a_non_string_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(
            ConfigError,
            match=r"connections\.primary\.dbt_manifest: expected a non-empty string, got 7",
        ):
            self._load(
                tmp_path,
                "connections:\n  primary:\n    adapter: duckdb\n    dbt_manifest: 7\n",
            )

    def test_under_defaults_it_is_refused_with_the_move(self, tmp_path: Path) -> None:
        with pytest.raises(
            ConfigError,
            match=r"defaults\.dbt_manifest: unknown key; it is set per connection",
        ):
            self._load(
                tmp_path,
                "defaults:\n  dbt_manifest: target/manifest.json\n"
                "connections:\n  primary:\n    adapter: duckdb\n",
            )


class TestStrictness:
    """Every key the schema does not name is refused, at any depth, all in one pass."""

    @pytest.mark.parametrize(
        ("body", "path"),
        [
            ("colour: blue\nconnections:\n  w: {adapter: postgres}\n", "colour"),
            ("connections:\n  w: {adapter: postgres, colour: blue}\n", "connections.w.colour"),
            (
                "connections:\n  w:\n    adapter: postgres\n    run: {colour: blue}\n",
                "connections.w.run.colour",
            ),
            (
                (
                    "connections:\n  w:\n    adapter: postgres\n"
                    "    profile: {statistics: {colour: blue}}\n"
                ),
                "connections.w.profile.statistics.colour",
            ),
            (
                (
                    "connections:\n  w:\n    adapter: postgres\n"
                    "    rules: [{match: {colour: blue}, profile: {freshness: {max_age_days: 1}}}]\n"
                ),
                "connections.w.rules[0].match.colour",
            ),
            (
                (
                    "defaults:\n  diff: {stat_change_threshold: {colour: 0.1}}\n"
                    "connections:\n  w: {adapter: postgres}\n"
                ),
                "defaults.diff.stat_change_threshold.colour",
            ),
        ],
    )
    def test_an_unknown_key_is_refused_with_its_whole_path(
        self,
        tmp_path: Path,
        body: str,
        path: str,
    ) -> None:
        with pytest.raises(ConfigError, match=re.escape(f"{path}: unknown key")):
            load_project(_write_config(tmp_path, body))

    def test_a_typo_is_answered_with_the_nearest_key(self, tmp_path: Path) -> None:
        body = "connections:\n  w:\n    adapter: postgres\n    profile: {statistcs: {}}\n"

        with pytest.raises(ConfigError) as exc:
            load_project(_write_config(tmp_path, body))

        assert str(exc.value).endswith(
            "connections.w.profile.statistcs: unknown key; did you mean 'statistics'?",
        )

    def test_a_key_in_the_wrong_place_is_pointed_at_its_home(self, tmp_path: Path) -> None:
        body = "connections:\n  w:\n    adapter: postgres\n    max_age_days: 1\n"

        with pytest.raises(ConfigError) as exc:
            load_project(_write_config(tmp_path, body))

        assert str(exc.value).endswith(
            "connections.w.max_age_days: unknown key; "
            "did you mean 'profile.freshness.max_age_days'?",
        )

    def test_every_problem_is_reported_in_one_run_in_document_order(self, tmp_path: Path) -> None:
        body = (
            "connections:\n  w:\n    adapter: postgres\n"
            "    run: {parallelism: two}\n"
            "    profile:\n      statistcs: {}\n      freshness: {max_age_days: -1}\n"
        )

        with pytest.raises(ConfigError) as exc:
            load_project(_write_config(tmp_path, body))

        lines = str(exc.value).splitlines()

        assert lines[0].endswith(".dbprint.yaml: 3 problems:")
        assert lines[1:] == [
            "  connections.w.run.parallelism: expected an integer of at least 1, got 'two'",
            "  connections.w.profile.statistcs: unknown key; did you mean 'statistics'?",
            (
                "  connections.w.profile.freshness.max_age_days: "
                "expected an integer of at least 0, got -1"
            ),
        ]

    def test_a_whole_float_is_read_as_an_integer(self, tmp_path: Path) -> None:
        body = "connections:\n  w:\n    adapter: postgres\n    profile: {freshness: {max_age_days: 7.0}}\n"
        conn = load_project(_write_config(tmp_path, body)).connections["w"]

        assert conn.profile.freshness.max_age_days == 7
        assert isinstance(conn.profile.freshness.max_age_days, int)

    def test_x_shared_holds_anchors_and_is_never_read(self, tmp_path: Path) -> None:
        body = """\
x-shared:
  anything: {at: all, nested: [1, 2]}
  mask_contact: &mask_contact {sensitivity: [contact], with: mask}
connections:
  w:
    adapter: postgres
    profile:
      redact:
        - *mask_contact
        - columns: [a.b.c]
          with: drop
"""
        conn = load_project(_write_config(tmp_path, body)).connections["w"]

        assert [(r.sensitivity, r.with_) for r in conn.profile.redact] == [
            (("contact",), "mask"),
            ((), "drop"),
        ]


class TestTheOneMergeRule:
    def test_a_defaults_rule_and_a_connection_rule_both_apply_in_order(
        self,
        tmp_path: Path,
    ) -> None:
        body = """\
defaults:
  rules:
    - match: {tables: [a.b]}
      profile: {statistics: {top_n_values: 5, enumeration_threshold: 9}}
connections:
  w:
    adapter: postgres
    rules:
      - match: {tables: [a.b]}
        profile: {statistics: {top_n_values: 7}}
"""
        settings = (
            load_project(_write_config(tmp_path, body))
            .connections["w"]
            .settings_for("a.b", "table")
        )

        assert settings.matched_rules == ("defaults.rules[0]", "connections.w.rules[0]")
        assert settings.statistics.top_n_values == 7
        assert settings.statistics.enumeration_threshold == 9

    def test_a_connection_list_replaces_the_defaults_list(self, tmp_path: Path) -> None:
        body = """\
defaults:
  tables: {include: [a.*, b.*], exclude: [a.x]}
connections:
  w:
    adapter: postgres
    tables: {include: [c.*]}
"""
        tables = load_project(_write_config(tmp_path, body)).connections["w"].tables

        assert tables.include == ("c.*",)
        assert tables.exclude == ("a.x",)


class TestReadModes:
    """`profile.read`: one mode per object type, each parameter bound to the mode that takes it."""

    def _conn(self, tmp_path: Path, body: str) -> ConnectionConfig:
        return load_project(_write_config(tmp_path, body)).connections["w"]

    def _refused(self, tmp_path: Path, body: str) -> str:
        with pytest.raises(ConfigError) as exc:
            load_project(_write_config(tmp_path, body))

        return str(exc.value)

    def test_tables_are_read_whole_and_views_described_by_default(self, tmp_path: Path) -> None:
        conn = self._conn(tmp_path, "connections:\n  w: {adapter: postgres}\n")
        table, view = conn.settings_for("a.b", "table"), conn.settings_for("a.v", "view")

        assert (table.mode, table.sample, table.filter, table.explicit) == (
            "full",
            None,
            None,
            False,
        )
        assert view.mode == "catalog"

    def test_a_views_block_under_min_rows_is_refused(self, tmp_path: Path) -> None:
        message = self._refused(
            tmp_path,
            "connections:\n  w:\n    adapter: postgres\n"
            "    rules: [{match: {min_rows: 5}, profile: {read: {views: {mode: full}}}}]\n",
        )

        assert "connections.w.rules[0]: sets profile.read.views with match.min_rows" in message

    def test_a_defaults_rule_is_checked_once_against_its_inherited_mode(
        self,
        tmp_path: Path,
    ) -> None:
        message = self._refused(
            tmp_path,
            "defaults:\n  rules: [{profile: {read: {tables: {fraction: 0.5}}}}]\n"
            "connections:\n  w: {adapter: postgres}\n  x: {adapter: postgres}\n",
        )

        assert message.count("defaults.rules[0].profile.read.tables.fraction: not valid") == 1

    def test_a_rule_samples_by_fraction(self, tmp_path: Path) -> None:
        conn = self._conn(
            tmp_path,
            "connections:\n  w:\n    adapter: postgres\n"
            "    rules: [{match: {tables: [a.b]}, profile: {read: {tables: {mode: sample, fraction: 0.01, materialize: false}}}}]\n",
        )
        settings = conn.settings_for("a.b", "table")

        assert (settings.mode, settings.sample, settings.materialize) == ("sample", 0.01, False)
        assert settings.explicit is True
        assert conn.settings_for("a.c", "table").sample is None

    def test_a_ceiling_samples_a_larger_table_and_reads_a_smaller_one_whole(
        self,
        tmp_path: Path,
    ) -> None:
        conn = self._conn(
            tmp_path,
            "connections:\n  w:\n    adapter: postgres\n"
            "    profile: {read: {tables: {mode: sample, max_rows: 1000}}}\n",
        )
        big = conn.settings_for("a.b", "table", 10_000)
        small = conn.settings_for("a.b", "table", 500)

        assert big.max_rows_scanned == 1000
        assert big.sample is not None
        assert big.sample <= 0.1
        assert (small.sample, small.max_rows_scanned) == (None, 1000)

    def test_a_rule_switching_to_filter_drops_the_inherited_ceiling(self, tmp_path: Path) -> None:
        conn = self._conn(
            tmp_path,
            "connections:\n  w:\n    adapter: postgres\n"
            "    profile: {read: {tables: {mode: sample, max_rows: 1000}}}\n"
            "    rules: [{profile: {read: {tables: {mode: filter, filter: x > 1}}}}]\n",
        )
        settings = conn.settings_for("a.b", "table", 10_000)

        assert (settings.mode, settings.filter, settings.sample) == ("filter", "x > 1", None)
        assert settings.max_rows_scanned is None

    def test_a_fraction_written_by_a_rule_replaces_an_inherited_ceiling(
        self,
        tmp_path: Path,
    ) -> None:
        conn = self._conn(
            tmp_path,
            "connections:\n  w:\n    adapter: postgres\n"
            "    profile: {read: {tables: {mode: sample, max_rows: 1000}}}\n"
            "    rules: [{profile: {read: {tables: {fraction: 0.5}}}}]\n",
        )
        settings = conn.settings_for("a.b", "table", 10_000)

        assert (settings.sample, settings.max_rows_scanned, settings.explicit) == (0.5, None, False)

    def test_a_rule_reads_views_whole(self, tmp_path: Path) -> None:
        conn = self._conn(
            tmp_path,
            "connections:\n  w:\n    adapter: postgres\n"
            "    rules: [{match: {tables: [a.v]}, profile: {read: {views: {mode: full}}}}]\n",
        )

        assert conn.settings_for("a.v", "view").mode == "full"
        assert conn.settings_for("a.w", "view").mode == "catalog"
        assert conn.settings_for("a.v", "table").mode == "full"

    def test_a_parameter_under_another_written_mode_is_refused(self, tmp_path: Path) -> None:
        message = self._refused(
            tmp_path,
            "connections:\n  w:\n    adapter: postgres\n"
            "    profile: {read: {tables: {mode: filter, fraction: 0.1}}}\n",
        )

        assert message.endswith(
            "connections.w.profile.read.tables.fraction: not valid under mode: filter; "
            "mode: filter takes filter",
        )

    def test_a_parameter_under_an_inherited_mode_is_refused(self, tmp_path: Path) -> None:
        message = self._refused(
            tmp_path,
            "connections:\n  w:\n    adapter: postgres\n"
            "    rules: [{profile: {read: {tables: {fraction: 0.1}}}}]\n",
        )

        assert message.endswith(
            "connections.w.rules[0].profile.read.tables.fraction: not valid under mode: full "
            "(inherited from built-in default); set mode: sample in this block",
        )

    @pytest.mark.parametrize(
        ("connection", "earlier", "later", "answer"),
        [
            (
                "{mode: sample, fraction: 0.1}",
                '{mode: filter, filter: "x > 1"}',
                "{mode: sample}",
                "mode: sample needs fraction or max_rows in a rule's own block",
            ),
            (
                '{mode: filter, filter: "x > 1"}',
                "{mode: sample, fraction: 0.1}",
                "{mode: filter}",
                "mode: filter needs filter in a rule's own block",
            ),
        ],
    )
    def test_a_rule_naming_a_mode_carries_its_own_parameters(
        self,
        tmp_path: Path,
        connection: str,
        earlier: str,
        later: str,
        answer: str,
    ) -> None:
        message = self._refused(
            tmp_path,
            "connections:\n  w:\n    adapter: postgres\n"
            f"    profile: {{read: {{tables: {connection}}}}}\n"
            "    rules:\n"
            f"    - {{match: {{tables: [a.b.c]}}, profile: {{read: {{tables: {earlier}}}}}}}\n"
            f"    - {{match: {{tables: [a.b.*]}}, profile: {{read: {{tables: {later}}}}}}}\n",
        )

        assert message.endswith(f"connections.w.rules[1].profile.read.tables: {answer}")

    def test_a_rule_restating_the_connection_mode_without_parameters_is_refused(
        self,
        tmp_path: Path,
    ) -> None:
        message = self._refused(
            tmp_path,
            "connections:\n  w:\n    adapter: postgres\n"
            "    profile: {read: {tables: {mode: sample, fraction: 0.1}}}\n"
            "    rules: [{profile: {read: {tables: {mode: sample}}}}]\n",
        )

        assert message.endswith(
            "connections.w.rules[0].profile.read.tables: mode: sample needs fraction or max_rows "
            "in a rule's own block",
        )

    @pytest.mark.parametrize(
        ("connection", "rule", "expected"),
        [
            (
                "{mode: sample, fraction: 0.1}",
                "{mode: sample, fraction: 0.2}",
                ("sample", 0.2, None),
            ),
            ("{mode: sample, fraction: 0.1}", "{fraction: 0.2}", ("sample", 0.2, None)),
            ("{mode: full}", '{mode: filter, filter: "y > 2"}', ("filter", None, "y > 2")),
        ],
    )
    def test_a_rule_carrying_its_own_parameters_is_accepted(
        self,
        tmp_path: Path,
        connection: str,
        rule: str,
        expected: tuple[str, float | None, str | None],
    ) -> None:
        conn = self._conn(
            tmp_path,
            "connections:\n  w:\n    adapter: postgres\n"
            f"    profile: {{read: {{tables: {connection}}}}}\n"
            f"    rules: [{{profile: {{read: {{tables: {rule}}}}}}}]\n",
        )
        settings = conn.settings_for("a.b", "table", 10_000)

        assert (settings.mode, settings.sample, settings.filter) == expected

    @pytest.mark.parametrize(
        ("block", "answer"),
        [
            (
                "{tables: {mode: sampled}}",
                (
                    "tables.mode: expected one of 'full', 'sample', 'filter', 'catalog', got "
                    "'sampled'; did you mean 'sample'?"
                ),
            ),
            ("{tables: {mode: sample}}", "tables: mode: sample needs fraction or max_rows"),
            ("{tables: {mode: filter}}", "tables: mode: filter needs filter"),
            (
                "{tables: {mode: sample, fraction: 0.1, max_rows: 5}}",
                "tables: sets both fraction and max_rows - a sample is bounded by one or the other",
            ),
            (
                "{views: {mode: sample}}",
                (
                    "views.mode: sample is not valid for views - a sampling clause binds to a "
                    "table; use mode: filter"
                ),
            ),
            (
                "{views: {mode: full, max_rows: 5}}",
                (
                    "views.max_rows: not valid for views - a view has no row-count estimate to "
                    "resolve a ceiling against"
                ),
            ),
        ],
    )
    def test_a_malformed_block_is_named(self, tmp_path: Path, block: str, answer: str) -> None:
        message = self._refused(
            tmp_path,
            f"connections:\n  w:\n    adapter: postgres\n    profile: {{read: {block}}}\n",
        )

        assert message.endswith(f"connections.w.profile.read.{answer}")


class TestResolvedOrigins:
    """`resolve_profile` names the layer that set every leaf, and every rule's verdict."""

    def _conn(self, tmp_path: Path) -> ConnectionConfig:
        _write_config(
            tmp_path,
            """\
defaults:
  profile:
    statistics: {top_n_values: 40}
  rules:
  - match: {min_rows: 1000}
    profile: {freshness: {max_age_days: 30}}
connections:
  w:
    adapter: postgres
    profile:
      redact: [{columns: ["*.email"]}]
      timeline: {enabled: false}
    rules:
    - match: {tables: [public.herbarium]}
      profile:
        statistics: {top_n_values: 5}
        read: {tables: {mode: sample, fraction: 0.5}}
""",
        )

        return load_project(tmp_path).connections["w"]

    def test_each_layer_is_named_where_it_wins(self, tmp_path: Path) -> None:
        origins = self._conn(tmp_path).resolve_profile("public.herbarium", 5000).origins

        assert origins[("statistics", "enumeration_threshold")] == "built-in default"
        assert origins[("timeline", "enabled")] == "connections.w.profile"
        assert origins[("freshness", "max_age_days")] == "defaults.rules[0]"
        assert origins[("statistics", "top_n_values")] == "connections.w.rules[0]"
        assert origins[("read", "tables", "fraction")] == "connections.w.rules[0]"
        assert origins[("read", "views", "mode")] == "built-in default"
        assert origins[("redact",)] == "connections.w.profile"

    def test_a_defaults_value_no_later_layer_touches_keeps_its_origin(self, tmp_path: Path) -> None:
        resolved = self._conn(tmp_path).resolve_profile("public.other")

        assert resolved.values["statistics"]["top_n_values"] == 40
        assert resolved.origins[("statistics", "top_n_values")] == "defaults.profile"

    def test_every_rule_reports_why_it_did_or_did_not_apply(self, tmp_path: Path) -> None:
        conn = self._conn(tmp_path)

        assert [v.outcome for v in conn.resolve_profile("public.herbarium", 5000).rules] == [
            "applied",
            "applied",
        ]
        assert [v.outcome for v in conn.resolve_profile("public.herbarium", 10).rules] == [
            "below_min_rows",
            "applied",
        ]
        assert [v.outcome for v in conn.resolve_profile("public.other").rules] == [
            "size_unknown",
            "name_mismatch",
        ]

    def test_no_table_resolves_no_rule_layer(self, tmp_path: Path) -> None:
        resolved = self._conn(tmp_path).resolve_profile(None)

        assert resolved.rules == ()
        assert resolved.values["statistics"]["top_n_values"] == 40


def test_a_parameter_an_earlier_rule_made_foreign_stays_inert(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        """\
connections:
  w:
    adapter: postgres
    profile: {read: {tables: {mode: sample, fraction: 0.1}}}
    rules:
    - match: {tables: [a.b]}
      profile: {read: {tables: {mode: full}}}
    - match: {tables: [a.*]}
      profile: {read: {tables: {fraction: 0.2}}}
""",
    )
    conn = load_project(tmp_path).connections["w"]
    settings = conn.settings_for("a.b", "table")

    assert (settings.mode, settings.sample) == ("full", None)
    assert conn.resolve_profile("a.b").values["read"]["tables"] == {"mode": "full"}
    assert conn.settings_for("a.c", "table").sample == 0.2  # noqa: RUF069 - the expected value is an exact literal


class TestTheBuiltInMaterialize:
    """A sample's unwritten `materialize` resolves once, at the end of the fold, as a built-in."""

    def _conn(self, tmp_path: Path, body: str) -> ConnectionConfig:
        _write_config(tmp_path, body)

        return load_project(tmp_path).connections["w"]

    def test_a_defaults_sample_credits_the_built_in(self, tmp_path: Path) -> None:
        conn = self._conn(
            tmp_path,
            "defaults:\n  profile:\n    read:\n      tables: {mode: sample, fraction: 0.1}\n"
            "connections:\n  w:\n    adapter: postgres\n",
        )
        resolved = conn.resolve_profile("public.herbarium")

        assert resolved.values["read"]["tables"]["materialize"] is True
        assert resolved.origins[("read", "tables", "materialize")] == "built-in default"
        assert conn.settings_for("public.herbarium", "table").materialize is True

    def test_a_rule_switching_to_sample_lists_the_materialize_generate_applies(
        self,
        tmp_path: Path,
    ) -> None:
        conn = self._conn(
            tmp_path,
            "connections:\n  w:\n    adapter: postgres\n    rules:\n"
            "    - match: {tables: ['*']}\n"
            "      profile: {read: {tables: {mode: sample, fraction: 0.1}}}\n",
        )
        resolved = conn.resolve_profile("public.herbarium")

        assert resolved.values["read"]["tables"]["materialize"] is True
        assert resolved.origins[("read", "tables", "materialize")] == "built-in default"
        assert conn.settings_for("public.herbarium", "table").materialize is True

    def test_a_written_materialize_keeps_its_layer(self, tmp_path: Path) -> None:
        conn = self._conn(
            tmp_path,
            "connections:\n  w:\n    adapter: postgres\n    profile:\n      read:\n"
            "        tables: {mode: sample, fraction: 0.1, materialize: false}\n",
        )
        resolved = conn.resolve_profile("public.herbarium")

        assert resolved.values["read"]["tables"]["materialize"] is False
        assert resolved.origins[("read", "tables", "materialize")] == "connections.w.profile"
        assert conn.settings_for("public.herbarium", "table").materialize is False
