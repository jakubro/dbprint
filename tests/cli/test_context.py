"""dbprint context - CLI command tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from dbprint.cli.main import main


PROJECT_YAML = """\
connections:
  production:
    adapter: postgres
    output: prints
"""


def _write_project(tmp_path: Path) -> None:
    (tmp_path / ".dbprint.yaml").write_text(PROJECT_YAML)


class TestSelection:
    def test_exact_fqn(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_project(tmp_path)
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(main, ["context", "seedbank.accession"])
        assert result.exit_code == 0
        assert "# Table: seedbank.accession" in result.output

    def test_pattern_matches_multiple(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_project(tmp_path)
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(main, ["context", "seedbank.*"])
        assert result.exit_code == 0
        assert "# Table: seedbank.accession" in result.output
        assert "# Table: seedbank.taxon" in result.output

    def test_all_includes_every_table(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_project(tmp_path)
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(main, ["context", "--all"])
        assert result.exit_code == 0
        assert "# Table: seedbank.accession" in result.output
        assert "# Table: seedbank.taxon" in result.output
        assert "# Table: fixture.shape_probe" in result.output

    def test_no_match_exact_errors_with_hint(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_project(tmp_path)
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(main, ["context", "seedbank.accessio"])
        assert result.exit_code != 0
        # Levenshtein hint should propose the closest match
        combined = result.output + (result.stderr or "")
        assert "Did you mean: seedbank.accession" in combined

    def test_no_match_pattern_errors(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_project(tmp_path)
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(main, ["context", "missing.*"])
        assert result.exit_code != 0


class TestFlags:
    def test_no_target_and_no_all_errors(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_project(tmp_path)
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(main, ["context"])
        assert result.exit_code != 0

    def test_format_json_emits_object(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_project(tmp_path)
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(
            main,
            ["context", "seedbank.accession", "--format", "json"],
        )
        assert result.exit_code == 0
        assert result.output.lstrip().startswith("{")

    def test_no_ddl_omits_ddl_section(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_project(tmp_path)
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(main, ["context", "seedbank.accession", "--no-ddl"])
        assert result.exit_code == 0
        assert "## DDL" not in result.output

    def test_no_annotations_omits_annotations_section(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`seedbank.accession` already ships `statistics.annotations.yaml` - nothing to seed."""

        _write_project(tmp_path)
        monkeypatch.chdir(tmp_path)

        with_section = CliRunner().invoke(main, ["context", "seedbank.accession"])
        assert "## Annotations" in with_section.output

        without_section = CliRunner().invoke(
            main,
            ["context", "seedbank.accession", "--no-annotations"],
        )
        assert without_section.exit_code == 0
        assert "## Annotations" not in without_section.output


class TestBudget:
    """`--budget` means the same thing on md, json and yaml."""

    def test_json_is_truncated_and_carries_the_marker(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_project(tmp_path)
        monkeypatch.chdir(tmp_path)
        unbudgeted = CliRunner().invoke(
            main,
            ["context", "seedbank.accession", "--format", "json"],
        )
        budgeted = CliRunner().invoke(
            main,
            ["context", "seedbank.accession", "--format", "json", "--budget", "20"],
        )

        assert budgeted.exit_code == 0
        assert len(budgeted.output) < len(unbudgeted.output)
        assert "_truncated" in yaml.safe_load(budgeted.output)

    def test_yaml_is_truncated_and_carries_the_marker(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_project(tmp_path)
        monkeypatch.chdir(tmp_path)
        unbudgeted = CliRunner().invoke(
            main,
            ["context", "seedbank.accession", "--format", "yaml"],
        )
        budgeted = CliRunner().invoke(
            main,
            ["context", "seedbank.accession", "--format", "yaml", "--budget", "20"],
        )

        assert budgeted.exit_code == 0
        assert len(budgeted.output) < len(unbudgeted.output)
        assert "_truncated" in yaml.safe_load(budgeted.output)

    def test_budget_too_small_exits_one_on_json(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_project(tmp_path)
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(
            main,
            ["context", "--all", "--format", "json", "--budget", "1"],
        )

        assert result.exit_code == 1
        assert "budget too small" in (result.output + (result.stderr or ""))

    def test_unbudgeted_json_is_unchanged(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_project(tmp_path)
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(
            main,
            ["context", "seedbank.accession", "--format", "json"],
        )
        payload = yaml.safe_load(result.output)

        assert result.exit_code == 0
        assert "_truncated" not in payload
        assert "ddl" in payload
        assert "statistics" in payload
        assert "relationships" in payload

    def test_unbudgeted_markdown_is_unchanged(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_project(tmp_path)
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(
            main,
            ["context", "seedbank.accession", "--budget", "4000"],
        )

        assert result.exit_code == 0
        assert "## DDL" in result.output
        assert "## Cardinality" in result.output
        assert "truncated:" not in result.output


class TestOutput:
    def test_output_flag_writes_file(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_project(tmp_path)
        monkeypatch.chdir(tmp_path)
        out_file = tmp_path / "out.md"
        result = CliRunner().invoke(
            main,
            ["context", "seedbank.accession", "--output", str(out_file)],
        )
        assert result.exit_code == 0
        assert out_file.is_file()
        assert "# Table: seedbank.accession" in out_file.read_text()


class TestMissingManifest:
    def test_no_manifest_exits_nonzero(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_project(tmp_path)
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(main, ["context", "--all"])
        assert result.exit_code != 0


class TestAWronglyShapedPrintDegradesRatherThanFails:
    """`context` assembles what it can and names the file it could not use, not the root."""

    def test_the_message_names_the_file_rather_than_claiming_it_is_absent(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _write_project(tmp_path)
        manifest = committed_print / "production" / "manifest.yaml"
        manifest.write_text("- one\n- two\n")
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(main, ["context", "--all"])

        assert result.exit_code == 1
        assert "no manifest at" not in result.output
        assert str(manifest) in result.output
        assert "list" in result.output or "mapping" in result.output

    def test_a_corrupt_statistics_file_costs_its_own_section_only(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A corrupt statistics.yaml must not abort the whole render, only its own section."""

        _write_project(tmp_path)
        broken = "seedbank.accession"
        stats = committed_print / "production" / broken.replace(".", "/") / "statistics.yaml"
        stats.write_text("{ not: valid")
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(main, ["context", "--all"])
        fragments = {
            fragment.splitlines()[0].strip(): fragment
            for fragment in result.stdout.split("# Table: ")[1:]
        }

        assert result.exit_code == 0, result.output
        assert "Cardinality" not in fragments[f"{broken}  (2,500 rows, 16 columns)"]
        assert "Cardinality" in fragments["seedbank.taxon  (300 rows, 8 columns)"]


class TestCatalogOnlyView:
    """SPEC 2.2.15: a view's context lists columns, never a fabricated cardinality table."""

    def test_columns_listed_no_cardinality_table(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`seedbank.accession_summary` is the print's real catalog-only view (SPEC 2.2.15)."""

        _write_project(tmp_path)
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(main, ["context", "seedbank.accession_summary"])

        assert result.exit_code == 0, result.output
        assert "## Columns (not queried)" in result.output
        assert "| accession_code | character varying(24) | text |" in result.output
        assert "| viability_pct | numeric(5,2) | numeric |" in result.output
        assert "Cardinality" not in result.output

    def test_json_format_still_carries_the_raw_marker(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The structured format is a pass-through - the marker itself stays visible there."""

        _write_project(tmp_path)
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(
            main,
            ["context", "seedbank.accession_summary", "--format", "json"],
        )
        payload = json.loads(result.output)

        assert result.exit_code == 0, result.output
        assert payload["statistics"]["catalog_only"] is True
