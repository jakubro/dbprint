"""`check_tty.render_human` - the offline human report's grouped sections."""

from __future__ import annotations

from io import StringIO

from dbprint.cli.rendering.check_data import CheckResult
from dbprint.cli.rendering.check_tty import render_human
from dbprint.conformance import Issue
from dbprint.engine.freshness import StaleEntry


def _result(*, issues: tuple[Issue, ...] = ()) -> CheckResult:
    return CheckResult(
        connection_name="primary",
        print_root="prints/primary",
        manifest_present=True,
        issues=issues,
        stale_entries=(),
        default_max_age_days=7.0,
        exit_code=1 if any(i.severity == "error" for i in issues) else 0,
    )


class TestWarningCountSurvivesTheErrorBranch:
    def test_errors_and_warnings_together_states_both_counts(self) -> None:
        result = _result(
            issues=(
                Issue("t1", "code.a", "error", "boom", "§1"),
                Issue("t2", "code.b", "warning", "heads up", "§2"),
                Issue("t3", "code.c", "warning", "also this", "§3"),
            ),
        )
        buf = StringIO()

        render_human([result], buf)

        assert "FAIL: conformance (1 error(s), 2 warning(s))" in buf.getvalue()

    def test_errors_with_no_warnings_carries_no_trailing_comma(self) -> None:
        result = _result(issues=(Issue("t1", "code.a", "error", "boom", "§1"),))
        buf = StringIO()

        render_human([result], buf)

        assert "FAIL: conformance (1 error(s))" in buf.getvalue()

    def test_warnings_with_no_errors_still_reads_ok(self) -> None:
        result = _result(issues=(Issue("t2", "code.b", "warning", "heads up", "§2"),))
        buf = StringIO()

        render_human([result], buf)

        assert "OK: conformance clean (1 warning(s))" in buf.getvalue()


class TestNoManifestNamesTheRealPath:
    def test_a_non_default_output_names_its_own_configured_location(self) -> None:
        """`prints/` is not every project's output directory - the failure must name where
        this connection actually looked, not a hardcoded guess.
        """

        result = CheckResult(
            connection_name="primary",
            print_root="warehouse_prints/primary",
            manifest_present=False,
            issues=(),
            stale_entries=(),
            default_max_age_days=7.0,
            exit_code=1,
        )
        buf = StringIO()

        render_human([result], buf)

        assert "FAIL: no manifest at warehouse_prints/primary/manifest.yaml" in buf.getvalue()


class TestFreshnessVerdictIsUnconditional:
    """A conformance error must not silence the freshness verdict, and an unmeasurable age
    must never count as an exceedance.
    """

    def test_freshness_verdict_appears_alongside_a_conformance_error(self) -> None:
        result = CheckResult(
            connection_name="primary",
            print_root="prints/primary",
            manifest_present=True,
            issues=(Issue("t1", "code.a", "error", "boom", "§1"),),
            stale_entries=(),
            default_max_age_days=7.0,
            exit_code=1,
        )
        buf = StringIO()

        render_human([result], buf)

        output = buf.getvalue()

        assert "FAIL: conformance (1 error(s))" in output
        assert "OK: every print is within its max-age threshold" in output

    def test_an_unmeasurable_age_is_a_note_not_an_exceedance(self) -> None:
        result = CheckResult(
            connection_name="primary",
            print_root="prints/primary",
            manifest_present=True,
            issues=(),
            stale_entries=(StaleEntry(fqn="public.t", age_days=float("inf"), max_age_days=7.0),),
            default_max_age_days=7.0,
            exit_code=2,
        )
        buf = StringIO()

        render_human([result], buf)

        output = buf.getvalue()

        assert "NOTE: 1 print(s) have an unmeasurable age" in output
        assert "exceed their max-age" not in output
        assert "OK: every print is within its max-age threshold" in output

    def test_a_measured_exceedance_and_an_unmeasurable_entry_are_counted_separately(self) -> None:
        result = CheckResult(
            connection_name="primary",
            print_root="prints/primary",
            manifest_present=True,
            issues=(),
            stale_entries=(
                StaleEntry(fqn="public.exceeded", age_days=30.0, max_age_days=7.0),
                StaleEntry(fqn="public.unmeasurable", age_days=float("inf"), max_age_days=7.0),
            ),
            default_max_age_days=7.0,
            exit_code=2,
        )
        buf = StringIO()

        render_human([result], buf)

        output = buf.getvalue()

        assert "FAIL: 1 print(s) exceed their max-age" in output
        assert "public.exceeded" in output
        assert "NOTE: 1 print(s) have an unmeasurable age" in output
        assert "public.unmeasurable" in output


class TestThresholdRendersInAcceptedForm:
    def test_a_half_day_threshold_renders_as_hours_not_a_fraction(self) -> None:
        """`--max-age` rejects `0.5d` - the threshold `check` prints must be a value the same
        flag would accept back, the same contract `format_age` already keeps for the age.
        """

        result = _result()
        result = CheckResult(
            connection_name=result.connection_name,
            print_root=result.print_root,
            manifest_present=result.manifest_present,
            issues=result.issues,
            stale_entries=(StaleEntry(fqn="public.t", age_days=1.0, max_age_days=0.5),),
            default_max_age_days=result.default_max_age_days,
            exit_code=2,
        )
        buf = StringIO()

        render_human([result], buf)

        assert "(max 12h)" in buf.getvalue()
        assert "0.5d" not in buf.getvalue()
