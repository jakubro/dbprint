"""dbprint annotate - the authoring brief and `--check`."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from click.testing import CliRunner

from dbprint.cli.main import main
from dbprint.cli.rendering.annotate_brief import render_brief
from dbprint.engine import OpenQuestion


PROJECT_YAML = """\
connections:
  production:
    adapter: postgres
    output: prints
"""

TWO_CONNECTION_YAML = """\
connections:
  production:
    adapter: postgres
    output: prints
    auto: true
  staging:
    adapter: postgres
    output: prints
    auto: true
"""

ACCESSION = "arboretum.seedbank.accession"
VAULT = "arboretum.seedbank.vault"
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")


@pytest.fixture
def project(tmp_path: Path, committed_print: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    del committed_print
    (tmp_path / ".dbprint.yaml").write_text(PROJECT_YAML)
    monkeypatch.chdir(tmp_path)

    return tmp_path


def _run(*args: str) -> tuple[int, str, str]:
    result = CliRunner().invoke(main, list(args))

    return result.exit_code, result.stdout, result.stderr


class TestTheBrief:
    def test_carries_steps_then_the_query_context_then_open_questions(self, project: Path) -> None:
        code, out, _ = _run("annotate", VAULT, "--no-tui")

        assert code == 0
        steps = out.index("# Annotating a dbprint print")
        context = out.index(f"# Table: {VAULT}")
        questions = out.index(f"## Open questions for {VAULT}")
        assert steps < context < questions
        assert "name: dbprint-annotate" not in out

    def test_its_context_is_byte_equal_to_context_purpose_query(self, project: Path) -> None:
        _, brief, _ = _run("annotate", ACCESSION, "--no-tui")
        _, context, _ = _run("context", ACCESSION, "--purpose", "query", "--no-tui")

        assert f"\n\n---\n\n{context.rstrip()}\n\n---\n\n" in brief

    def test_names_each_question_with_the_file_that_answers_it(self, project: Path) -> None:
        _, out, _ = _run("annotate", VAULT, "--no-tui")
        directory = "prints/production/arboretum/seedbank/vault"

        assert f"- No description: write `{directory}/description.md`" in out
        assert (
            "- `opens_at` lists codes with no meaning ('07:30:00', '08:00:00'): say what each "
            f"stands for in `{directory}/statistics.annotations.yaml` at "
            "`columns.opens_at.values`."
        ) in out

    def test_piped_is_plain_and_tui_is_styled(self, project: Path) -> None:
        _, piped, _ = _run("annotate", VAULT, "--no-tui")
        _, styled, _ = _run("annotate", VAULT, "--tui")

        assert not _ANSI_RE.search(piped)
        assert _ANSI_RE.search(styled)

    def test_says_so_when_nothing_is_open(self) -> None:
        text = render_brief("# Steps\n", "# Table: t\n", [], "t", "prints/w")

        assert text.endswith(
            "## Open questions for t\n\nNo open questions: the print records an answer for "
            "everything it can detect on this table.\n",
        )

    def test_spells_values_as_sql_literals(self) -> None:
        questions = [
            OpenQuestion(
                "frequent_value",
                "ratio",
                (1e-07,),
                None,
                "t/x.yaml",
                "columns.ratio.values",
            ),
            OpenQuestion(
                "codes",
                "label",
                ("a, b", "it's"),
                None,
                "t/x.yaml",
                "columns.label.values",
            ),
        ]
        text = render_brief("", "", questions, "t", "")

        assert "`ratio` holds 0.0000001 on a large share" in text
        assert "`label` lists codes with no meaning ('a, b', 'it''s')" in text


class TestSelection:
    def test_an_unknown_table_exits_1_as_context_does(self, project: Path) -> None:
        code, _, err = _run("annotate", "seedbank.accessio")
        _, _, context_err = _run("context", "seedbank.accessio")

        assert code == 1
        assert err == context_err
        assert "Did you mean: arboretum.seedbank.accession" in err

    def test_a_pattern_is_refused(self, project: Path) -> None:
        code, _, err = _run("annotate", "arboretum.seedbank.*")

        assert code == 1
        assert "not a pattern" in err

    def test_a_table_in_two_connections_needs_one_named(
        self,
        tmp_path: Path,
        committed_print: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / ".dbprint.yaml").write_text(TWO_CONNECTION_YAML)
        (committed_print / "staging").symlink_to(committed_print / "production")
        monkeypatch.chdir(tmp_path)
        code, _, err = _run("annotate", VAULT)

        assert code == 1
        assert "production, staging - pass CONNECTION" in err
        assert _run("annotate", VAULT, "staging", "--no-tui")[0] == 0


class TestCheck:
    def test_a_clean_table_exits_0(self, project: Path) -> None:
        code, out, _ = _run("annotate", ACCESSION, "--check")

        assert code == 0
        assert out == f"{ACCESSION}: no annotation findings\n"

    def test_a_contradicted_claim_exits_1_and_names_only_this_table(
        self,
        project: Path,
        committed_print: Path,
    ) -> None:
        root = committed_print / "production" / "arboretum" / "seedbank"
        claims = "format_version: 1\ncolumns:\n  vault_id:\n    claims:\n      null_rate: 0.5\n"
        (root / "accession" / "statistics.annotations.yaml").write_text(claims)
        (root / "vault" / "statistics.annotations.yaml").write_text(claims)
        code, out, _ = _run("annotate", ACCESSION, "--check")

        assert code == 1
        assert "arboretum/seedbank/accession/statistics.annotations.yaml" in out
        assert "annotations.claim-contradicts-statistic" in out
        assert "arboretum/seedbank/vault/" not in out

    def test_a_rejection_of_a_field_the_column_lacks_exits_1(
        self,
        project: Path,
        committed_print: Path,
    ) -> None:
        annotations = committed_print / "production/arboretum/seedbank/accession"
        (annotations / "statistics.annotations.yaml").write_text(
            "format_version: 1\ncolumns:\n  received_at:\n    rejected: [candidate_key]\n",
        )
        code, out, _ = _run("annotate", ACCESSION, "--check")

        assert code == 1
        assert "columns.received_at.rejected" in out
        assert "annotations.unknown-inference" in out

    def test_a_rejection_with_its_note_and_no_claim_exits_0(
        self,
        project: Path,
        committed_print: Path,
    ) -> None:
        annotations = committed_print / "production/arboretum/seedbank/accession"
        (annotations / "statistics.annotations.yaml").write_text(
            "format_version: 1\ncolumns:\n  collected_on:\n"
            "    note: Several accessions can share a collecting day.\n"
            "    rejected: [candidate_key]\n",
        )
        code, out, _ = _run("annotate", ACCESSION, "--check")

        assert code == 0
        assert out == f"{ACCESSION}: no annotation findings\n"
