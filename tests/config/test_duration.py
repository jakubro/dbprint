"""`config/duration.py` - the seconds form `statement_timeout` reads, on the grammar `--max-age` uses."""

from __future__ import annotations

import pytest

from dbprint.config.duration import (
    DurationError,
    format_duration_seconds,
    parse_duration_seconds,
)


class TestParseDurationSeconds:
    @pytest.mark.parametrize(
        ("text", "seconds"),
        [("30s", 30), ("10m", 600), ("2h", 7200), ("1d", 86400), ("10M", 600)],
    )
    def test_each_unit(self, text: str, seconds: int) -> None:
        assert parse_duration_seconds(text) == seconds

    @pytest.mark.parametrize("text", ["10", "1h30m", "m", "-5s", "1.5h", ""])
    def test_anything_else_is_refused(self, text: str) -> None:
        with pytest.raises(DurationError):
            parse_duration_seconds(text)


class TestFormatDurationSeconds:
    @pytest.mark.parametrize(
        ("seconds", "text"),
        [(600, "10m"), (7200, "2h"), (86400, "1d"), (90, "90s"), (5400, "90m")],
    )
    def test_the_largest_exact_unit(self, seconds: int, text: str) -> None:
        assert format_duration_seconds(seconds) == text

    @pytest.mark.parametrize("text", ["45s", "10m", "3h", "2d"])
    def test_a_parsed_value_renders_back_as_written(self, text: str) -> None:
        assert format_duration_seconds(parse_duration_seconds(text)) == text
