"""Detector behavior: the match threshold, small samples, and what counts as a sample.

Non-string driver values (uuid.UUID, Decimal, date) are pinned here too.
"""

from __future__ import annotations

import base64
import importlib.resources
import json
import uuid
from datetime import date
from decimal import Decimal
from typing import get_args

import pytest

from dbprint.spec.looks_like import (
    MATCH_THRESHOLD,
    NEAR_MISS_FLOOR,
    _CURRENCY_CODES,
    LooksLike,
    detect,
    detect_with_evidence,
)


# Sample size at which the threshold first admits a non-matching value (SPEC 4.1.3).
TOLERANCE_MIN_SAMPLES = 20


def _uuids(n: int) -> list[str]:
    """n distinct values matching the uuid pattern."""

    return [f"{i:08d}-0000-7000-8000-{i:012d}" for i in range(n)]


def _emails(n: int) -> list[str]:
    return [f"user{i}@example.com" for i in range(n)]


def _media_types(n: int) -> list[str]:
    registered = ["image/png", "image/jpeg", "application/pdf", "text/html", "video/mp4"]

    return [registered[i % len(registered)] for i in range(n)]


class TestEnumAgreement:
    """The Python `Literal` and the JSON Schema enum are hand-maintained twins."""

    def test_the_literal_and_the_schema_enum_carry_the_same_values(self) -> None:
        text = importlib.resources.files("dbprint.spec.v1").joinpath("statistics.schema.json")
        schema = json.loads(text.read_text())
        schema_values = set(schema["$defs"]["LooksLike"]["enum"])

        assert set(get_args(LooksLike)) == schema_values


class TestSmallSamples:
    """Every sample is inspected; the threshold is the only gate (SPEC 4.1.3, SPEC 2.2.4)."""

    def test_a_small_unanimous_column_is_detected(self) -> None:
        """Twelve distinct values are published in full, so they are also read."""

        assert detect(_uuids(12)) == "uuid"

    def test_one_stray_value_is_enough_to_withhold_a_verdict(self) -> None:
        """At this size the threshold admits no noise, which is the whole of the rule."""

        values = _uuids(11) + ["unknown"]

        assert (len(values) - 1) / len(values) < MATCH_THRESHOLD
        assert detect(values) is None

    def test_a_single_value_column_is_detected(self) -> None:
        """One observation is the extreme of the published band, and it is a stated verdict."""

        assert detect(_uuids(1)) == "uuid"

    def test_an_empty_sample_yields_nothing(self) -> None:
        assert detect([]) is None

    def test_non_strings_dilute_rather_than_vanish(self) -> None:
        """Counted in the denominator, stringified, not dropped before the tally (SPEC 4.1.3)."""

        values: list[object] = [*range(500), *_uuids(12)]

        assert detect(values) == "numeric_string"

    def test_a_numeric_column_is_detected_once_stringified(self) -> None:
        """Every int renders as `numeric_string` under its own default form."""

        assert detect(list(range(500))) == "numeric_string"


class TestNonStringValues:
    """Every pattern is defined over a string (SPEC 4.1.1); a driver's native type is coerced."""

    def test_a_native_uuid_object_is_detected(self) -> None:
        """The format's flagship pattern, reachable on the type it is named after."""

        values: list[object] = [uuid.UUID(f"{i:08d}-0000-7000-8000-{i:012d}") for i in range(30)]

        assert detect(values) == "uuid"

    def test_a_decimal_column_is_detected_as_numeric_string(self) -> None:
        values: list[object] = [Decimal(f"{i}.50") for i in range(30)]

        assert detect(values) == "numeric_string"

    def test_a_homogeneous_date_column_is_detected_via_its_default_rendering(self) -> None:
        """A native `date`'s `str()` form is `YYYY-MM-DD`, which `iso8601_date` now defines."""

        values: list[object] = [date(2024, 1, (i % 28) + 1) for i in range(30)]

        assert detect(values) == "iso8601_date"

    def test_a_mixed_json_sample_does_not_report_the_string_minority(self) -> None:
        """SPEC 4.1.3's own worked example: a jsonb column sampled alongside stray URL strings."""

        objects: list[object] = [{"id": i, "kind": "object"} for i in range(950)]
        url_strings: list[object] = [f"https://example.com/{i}" for i in range(50)]

        assert detect(objects + url_strings) is None

    def test_that_same_url_minority_alone_still_reports_url(self) -> None:
        """The control: it is the dilution that withholds a verdict, not the shape itself."""

        assert detect([f"https://example.com/{i}" for i in range(50)]) == "url"


class TestMatchThreshold:
    """The threshold tolerates data-quality noise; it does not tolerate a coin flip."""

    def test_noise_within_tolerance_still_detects(self) -> None:
        values = _uuids(TOLERANCE_MIN_SAMPLES - 1) + ["unknown"]

        assert len(values) == TOLERANCE_MIN_SAMPLES
        assert (TOLERANCE_MIN_SAMPLES - 1) / TOLERANCE_MIN_SAMPLES >= MATCH_THRESHOLD
        assert detect(values) == "uuid"

    def test_noise_beyond_tolerance_detects_nothing(self) -> None:
        values = _uuids(TOLERANCE_MIN_SAMPLES - 2) + ["unknown", "deleted"]

        assert detect(values) is None


class TestSampleSizeDoesNotChangeAVerdict:
    """A shape-uniform column reads the same whether it was sampled or not."""

    @pytest.mark.parametrize("size", [TOLERANCE_MIN_SAMPLES, 100, 1000])
    def test_uniform_shape_survives_any_sample_size(self, size: int) -> None:
        assert detect(_emails(size)) == "email"

    def test_a_sample_is_a_subset_and_reads_the_same(self) -> None:
        full = _emails(1000)

        assert detect(full) == detect(full[::25]) == "email"


class TestPriorityOrder:
    """The first matching pattern wins; the branch order is the spec order."""

    def test_uuid_outranks_the_patterns_below_it(self) -> None:
        assert detect(_uuids(TOLERANCE_MIN_SAMPLES)) == "uuid"

    def test_numeric_strings_are_detected_when_nothing_above_matches(self) -> None:
        assert detect([str(i * 7) for i in range(TOLERANCE_MIN_SAMPLES)]) == "numeric_string"

    def test_base64_requires_the_length_floor(self) -> None:
        """Short base64-shaped values are numeric or hex strings far more often."""

        assert detect(["dGVzdA=="] * TOLERANCE_MIN_SAMPLES) is None

    def test_base64_above_the_length_floor_is_detected(self) -> None:
        values = [
            base64.b64encode(f"payload-value-{i:04d}".encode()).decode()
            for i in range(TOLERANCE_MIN_SAMPLES)
        ]

        assert detect(values) == "base64"

    def test_base64_requires_a_case_mixture(self) -> None:
        """An all-lowercase or all-uppercase run clears the length floor and alphabet, so the
        case-mixture floor is what separates snake_case and hyphenated codes from real base64.
        """

        assert detect(_repeat("abcdefghijklmnop")) is None
        assert detect(_repeat("ABCDEFGHIJKLMNOP")) is None

    def test_base64_url_safe_requires_real_padding(self) -> None:
        """An unpadded URL-safe candidate matches only at a multiple-of-four length; 18 is not."""

        assert detect(_repeat("parcel-Handle-42XY")) is None

    def test_base64_url_safe_at_the_correct_length_is_detected(self) -> None:
        """The same shape two characters longer lands on a multiple of four."""

        assert detect(_repeat("parcel-Handle-42XYab")) == "base64"

    def test_json_requires_a_structured_top_level(self) -> None:
        """A bare primitive parses as JSON but is not what a consumer wants flagged."""

        assert detect(["42"] * TOLERANCE_MIN_SAMPLES) == "numeric_string"

    def test_structured_json_is_detected(self) -> None:
        assert detect([f'{{"tier": {i}}}' for i in range(TOLERANCE_MIN_SAMPLES)]) == "json"

    def test_no_pattern_reaches_threshold(self) -> None:
        assert detect([f"free text value {i}" for i in range(TOLERANCE_MIN_SAMPLES)]) is None


def _repeat(value: str, n: int = TOLERANCE_MIN_SAMPLES + 10) -> list[str]:
    """A sample wide enough for the threshold to have room in it, all of one value."""

    return [value] * n


# Canonical sample per LooksLike value (SPEC 4.1.4 priority order).
# Coverage is asserted against get_args(LooksLike), so an unregistered pattern fails loudly.
_CANONICAL_SAMPLES: tuple[tuple[LooksLike, str], ...] = (
    ("uuid", "00000000-0000-7000-8000-000000000000"),
    ("email", "person@example.com"),
    ("url", "https://example.com/resource"),
    ("urn", "urn:isbn:0451450523"),
    ("content_type", "application/json"),
    ("ip", "192.0.2.1"),
    ("mac_address", "00:1b:63:84:45:e6"),
    ("country_code", "US"),
    ("currency_code", "EUR"),
    ("postal_code", "SW1A 1AA"),
    ("isbn", "9780306406157"),
    ("ean", "036000291452"),
    ("imei", "352099001761481"),
    ("card_number", "4111111111111111"),
    ("iban", "DE89370400440532013000"),
    ("bic", "DEUTDEFF500"),
    ("phone", "+15551234567"),
    ("timezone", "Europe/London"),
    ("json", '{"id": 1}'),
    ("hex", "1A2B3C"),
    (
        "jwt",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk",
    ),
    ("vin", "1HGCM82633A004352"),
    ("base64", "Y2Fub25pY2FsLXBheWxvYWQtMDE="),
    ("latlon", "51.5074,-0.1278"),
    ("iso8601_duration", "P1Y2M3DT4H5M6S"),
    ("iso8601_date", "2024-01-15"),
    ("iso8601_datetime", "2024-01-15T09:30:00Z"),
    ("numeric_string", "42"),
    # filename-shaped: the final dot-segment starts with a letter; semver outranks it.
    ("semver", "1.0.0-alpha.beta"),
    # filename-shaped once a directory is stripped; path outranks filename.
    ("path", "a/image.png"),
    # hostname-shaped; there is no dedicated pattern, so filename wins.
    ("filename", "db.internal.example"),
    ("prose", "the quick brown fox jumps over the lazy dog"),
)


class TestSubsumptionMatrix:
    """One canonical sample per pattern; no higher-ranked pattern may steal it.

    `path` and `filename`'s samples also match a lower-ranked pattern (SPEC 4.1.4), so
    passing proves priority, not just a clean match.
    """

    def test_every_pattern_has_a_registered_sample(self) -> None:
        assert {row[0] for row in _CANONICAL_SAMPLES} == set(get_args(LooksLike))

    @pytest.mark.parametrize(
        ("pattern", "sample"),
        _CANONICAL_SAMPLES,
        ids=[row[0] for row in _CANONICAL_SAMPLES],
    )
    def test_the_expected_claimant_wins(self, pattern: str, sample: str) -> None:
        assert detect(_repeat(sample)) == pattern, f"expected {pattern} to claim {sample!r}"


class TestShapeDetectors:
    """The four patterns that describe how a value is shaped rather than what it means."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("image/png", "content_type"),
            ("application/pdf", "content_type"),
            ("text/html; charset=utf-8", "content_type"),
            ("x-custom/thing", "content_type"),
            ("/var/log/app.log", "path"),
            ("a/image.png", "path"),
            ("./relative/file.txt", "path"),
            ("report.pdf", "filename"),
            ("archive.tar.gz", "filename"),
            ("the quick brown fox jumps over the lazy dog", "prose"),
        ],
    )
    def test_each_pattern_is_detected(self, value: str, expected: str) -> None:
        assert detect(_repeat(value)) == expected

    @pytest.mark.parametrize(
        "value",
        ["C:\\Users\\jane\\file.txt", "\\\\server\\share\\file.txt"],
    )
    def test_windows_paths_are_not_matched(self, value: str) -> None:
        """v1 is POSIX-only, so a Windows path reports nothing rather than something wrong."""

        assert detect(_repeat(value)) is None


class TestShapeOverlaps:
    """Every one of these values satisfies more than one pattern; the order decides."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("https://x.com/a/img.png", "url"),
            ("image/png", "content_type"),
            ("a/image.png", "path"),
            ("image.png", "filename"),
            ("192.168.1.1", "ip"),
            ("1.5", "numeric_string"),
            ("-42", "numeric_string"),
            ("1.0.0-alpha.beta", "semver"),
        ],
    )
    def test_the_highest_ranked_match_wins(self, value: str, expected: str) -> None:
        assert detect(_repeat(value)) == expected


class TestOverlapsInAMixedColumn:
    """A value carries one assignment, so a subsuming pattern cannot outscore its senior."""

    def test_a_column_of_addresses_does_not_report_filename(self) -> None:
        """`filename` matches every dotted token with no separator, addresses included."""

        sample = _emails(90) + [f"example{i}.com" for i in range(10)]

        assert detect(sample) is None

    def test_those_addresses_alone_still_report_email(self) -> None:
        assert detect(_emails(100)) == "email"

    def test_a_column_of_media_types_does_not_report_path(self) -> None:
        """SPEC 4.1.4 names `content_type` above `path` so `image/png` is not two segments."""

        sample = _media_types(90) + [f"var/log/app{i}" for i in range(10)]

        assert detect(sample) is None

    def test_those_media_types_alone_still_report_content_type(self) -> None:
        assert detect(_media_types(100)) == "content_type"


class TestProseIsTheFallthrough:
    def test_short_labels_are_not_prose(self) -> None:
        """A categorical column of one-word labels must not read as free text."""

        assert detect(["active", "inactive", "archived"] * 10) is None

    def test_five_tokens_without_a_function_word_are_not_prose(self) -> None:
        assert detect(_repeat("alpha beta gamma delta epsilon")) is None

    def test_a_long_url_is_not_prose(self) -> None:
        """A long link carries enough tokens to read as free text, and ranks above it."""

        assert detect(_repeat("https://example.com/a/very/long/path/segment/here")) == "url"

    def test_prose_mixed_with_urls_reaches_no_verdict(self) -> None:
        """Two shapes at half the sample each, and neither is what the column is."""

        sample = ["the quick brown fox jumps over the lazy dog"] * 15
        sample += ["https://example.com/some/path"] * 15

        assert detect(sample) is None


class TestShapeThresholdBoundary:
    def test_noise_below_the_tolerance_still_matches(self) -> None:
        """96% prose - the 95% threshold absorbs the placeholder rows."""

        sample = ["the quick brown fox jumps over the lazy dog"] * 29 + ["placeholder"]

        assert detect(sample) == "prose"

    def test_noise_above_the_tolerance_does_not(self) -> None:
        sample = ["the quick brown fox jumps over the lazy dog"] * 28 + ["x", "y"]

        assert detect(sample) is None


class TestLocaleBoundPatterns:
    """`country_code` and `postal_code` are the two shapes that carry a locale."""

    @pytest.mark.parametrize("value", ["US", "gb", "DE", "JP", "ZA"])
    def test_iso_alpha2_is_detected(self, value: str) -> None:
        assert detect(_repeat(value)) == "country_code"

    def test_alpha3_is_not_detected(self) -> None:
        """v1 ships alpha-2 only; a second hand-maintained list is a silent-error surface."""

        assert detect(_repeat("USA")) is None

    def test_a_column_of_state_codes_does_not_reach_the_threshold(self) -> None:
        """The closed set is self-limiting: overlap with US states is about a third."""

        states = ["CA", "NY", "TX", "FL", "WA", "OR", "NV", "UT", "OH", "KY"]

        assert detect(states * 3) is None

    @pytest.mark.parametrize("value", ["SW1A 1AA", "K1A 0B1", "1234 AB", "EC1A1BB"])
    def test_self_identifying_postal_codes_are_detected(self, value: str) -> None:
        assert detect(_repeat(value)) == "postal_code"

    def test_an_all_digit_zip_reports_numeric_string(self) -> None:
        """A stated gap: five digits are indistinguishable from a number without a locale."""

        assert detect(_repeat("90210")) == "numeric_string"


class TestCurrencyList:
    """The hand-maintained ISO 4217 literal itself, not the detector built on it."""

    def test_the_list_has_178_entries(self) -> None:
        assert len(_CURRENCY_CODES) == 178

    @pytest.mark.parametrize("code", ["EUR", "USD", "JPY", "GBP", "XAU", "XDR", "XXX"])
    def test_a_well_known_code_is_a_member(self, code: str) -> None:
        assert code in _CURRENCY_CODES


class TestCurrencyCode:
    @pytest.mark.parametrize("value", ["EUR", "USD", "JPY"])
    def test_a_currency_code_is_detected(self, value: str) -> None:
        assert detect(_repeat(value)) == "currency_code"

    @pytest.mark.parametrize("value", ["XAU", "XDR", "XXX"])
    def test_metal_fund_and_test_codes_are_detected(self, value: str) -> None:
        """In the published list, so no exclusion clause carves them out."""

        assert detect(_repeat(value)) == "currency_code"

    @pytest.mark.parametrize("value", ["eur", "usd"])
    def test_a_lowercase_currency_code_is_detected(self, value: str) -> None:
        assert detect(_repeat(value)) == "currency_code"

    @pytest.mark.parametrize("value", ["USA", "DEU", "SVK"])
    def test_an_alpha3_country_code_is_not_detected(self, value: str) -> None:
        """Only 4 of 249 alpha-3 codes are also currency codes; no column reaches 0.95."""

        assert detect(_repeat(value)) is None

    def test_overlapping_language_codes_still_report_country_code(self) -> None:
        """`country_code` outranks `currency_code`; the overlap is a stated gap, not a fix."""

        sample = ["de", "es", "it", "pt"] * 8

        assert detect(sample) == "country_code"

    def test_mixed_language_codes_report_nothing(self) -> None:
        sample = ["en", "sk", "de", "fr"] * 8

        assert detect(sample) is None

    def test_a_bcp47_tag_is_not_detected(self) -> None:
        assert detect(_repeat("en-US")) is None


class TestTimezone:
    """A closed set drawn from the stdlib, not a hand-maintained literal."""

    @pytest.mark.parametrize(
        "value",
        ["Europe/London", "America/New_York", "America/Indiana/Knox", "UTC", "GMT", "EST"],
    )
    def test_a_real_zone_name_is_detected(self, value: str) -> None:
        assert detect(_repeat(value)) == "timezone"

    def test_a_16_character_zone_name_outranks_base64(self) -> None:
        """`Pacific/Auckland` is 16 characters and its alphabet is a base64 subset."""

        assert detect(_repeat("Pacific/Auckland")) == "timezone"

    def test_a_mixed_column_of_every_form_still_clears_the_threshold(self) -> None:
        sample = ["Europe/London", "America/New_York", "America/Indiana/Knox", "UTC"] * 8

        assert detect(sample) == "timezone"

    def test_a_real_filesystem_path_still_reports_path(self) -> None:
        assert detect(_repeat("/var/log/app.log")) == "path"

    def test_country_codes_still_outrank_timezone(self) -> None:
        """`GB` and `NZ` are IANA `backward` aliases on some hosts; `country_code` wins first."""

        assert detect(_repeat("GB")) == "country_code"
        assert detect(_repeat("DE")) == "country_code"

    def test_a_lowercased_zone_name_is_not_detected(self) -> None:
        """Membership is case-sensitive; a lowercased name is not the name of anything."""

        assert detect(_repeat("europe/london")) == "path"

    def test_a_utc_offset_is_not_detected(self) -> None:
        assert detect(_repeat("+02:00")) is None


class TestPhone:
    """E.164 and separator-bearing national forms; a bare digit run stays `numeric_string`."""

    @pytest.mark.parametrize(
        "value",
        ["+15551234567", "+442079460958", "+1 (555) 123-4567", "+44 20 7946 0958"],
    )
    def test_e164_forms_are_detected(self, value: str) -> None:
        assert detect(_repeat(value)) == "phone"

    @pytest.mark.parametrize("value", ["(555) 123-4567", "555-123-4567"])
    def test_national_separator_forms_are_detected(self, value: str) -> None:
        assert detect(_repeat(value)) == "phone"

    def test_a_column_mixing_e164_and_national_still_clears_the_threshold(self) -> None:
        """Both forms assign `phone`, unlike a column mixing formatted and bare."""

        sample = ["+15551234567"] * 15 + ["555-987-6543"] * 15

        assert detect(sample) == "phone"

    def test_a_bare_digit_run_stays_numeric_string(self) -> None:
        """No separator, no evidence - a phone number and an account id look identical."""

        assert detect(_repeat("5551234567")) == "numeric_string"

    def test_a_mix_of_formatted_and_bare_clears_neither_threshold(self) -> None:
        sample = ["+15551234567"] * 9 + ["5551234567"]

        assert detect(sample) is None

    def test_an_iso_date_is_not_a_phone(self) -> None:
        """Below the separator-form floor: eight digits with hyphens report `iso8601_date`."""

        assert detect(_repeat("2026-08-11")) == "iso8601_date"

    def test_a_us_ssn_is_not_a_phone(self) -> None:
        """Nine digits with hyphens sits below the separator-form floor."""

        assert detect(_repeat("123-45-6789")) is None

    @pytest.mark.parametrize("value", ["1.2.3", "10.14.2"])
    def test_a_version_string_is_not_a_phone(self, value: str) -> None:
        """The dot is not a recognized separator - it reports `semver` instead, never `phone`."""

        assert detect(_repeat(value)) == "semver"

    def test_a_15_digit_e164_number_outranks_base64(self) -> None:
        """`+123456789012345` is 16 characters and would decode as base64; `phone` outranks it."""

        assert detect(_repeat("+123456789012345")) == "phone"

    def test_a_card_number_outranks_phone(self) -> None:
        """An Amex number is a 15-digit separator-bearing run, same as a phone (SPEC 4.1.1).

        `card_number` outranks `phone`, and the Luhn check settles the collision.
        """

        assert detect(_repeat("3782 822463 10005")) == "card_number"


class TestFinancialIdentifiers:
    """The two shapes carrying a checksum: `card_number` (Luhn) and `iban` (mod-97)."""

    @pytest.mark.parametrize(
        "value",
        ["4111111111111111", "5500005555555559", "4111 1111 1111 1111"],
    )
    def test_a_luhn_valid_pan_is_detected(self, value: str) -> None:
        assert detect(_repeat(value)) == "card_number"

    def test_a_luhn_invalid_run_is_not_a_card_number(self) -> None:
        """One digit off a real PAN: the checksum fails and the all-digit value falls to
        `numeric_string`, since `base64`'s case-mixture floor declines it too.
        """

        assert detect(_repeat("4111111111111112")) == "numeric_string"

    def test_a_run_shorter_than_thirteen_digits_is_not_a_card_number(self) -> None:
        """`1111` alone - a masked or last-four rendering - carries no PAN."""

        assert detect(_repeat("1111")) == "numeric_string"

    def test_a_masked_pan_is_not_a_card_number(self) -> None:
        assert detect(_repeat("************1111")) is None

    def test_a_luhn_valid_order_id_column_does_not_reach_the_threshold(self) -> None:
        """~10% of a sequential-id column passes Luhn by chance; 0.10 cannot clear 0.95."""

        sample = [f"411111111100{i:04d}" for i in range(30)]
        share = sum(detect([v]) == "card_number" for v in sample) / len(sample)

        assert share < MATCH_THRESHOLD
        assert detect(sample) is None

    @pytest.mark.parametrize(
        "value",
        ["DE89370400440532013000", "SK3112000000198742637541"],
    )
    def test_a_valid_iban_is_detected(self, value: str) -> None:
        assert detect(_repeat(value)) == "iban"

    def test_a_spaced_iban_is_detected(self) -> None:
        """Spaces are stripped before the mod-97 check runs (SPEC 4.1.1)."""

        compact = "SK3112000000198742637541"
        spaced = " ".join(compact[i : i + 4] for i in range(0, len(compact), 4))

        assert detect(_repeat(spaced)) == "iban"

    def test_an_iban_with_a_bad_check_digit_is_not_detected(self) -> None:
        """One digit off a real IBAN: the mod-97 remainder no longer equals 1."""

        assert detect(_repeat("GB82WEST12345698765431")) is None

    def test_an_alpha3_country_code_column_does_not_reach_the_iban_threshold(self) -> None:
        """A closed set's own refusal of alpha-3 does not reappear through `iban`'s door."""

        assert detect(_repeat("USA")) is None

    @pytest.mark.parametrize("value", ["SEEDGB2L", "DEUTDEFF500"])
    def test_a_valid_bic_is_detected(self, value: str) -> None:
        assert detect(_repeat(value)) == "bic"

    def test_a_bic_with_an_unrecognised_country_is_not_detected(self) -> None:
        """Shape-correct but positions 5-6 are not an ISO 3166 alpha-2 code."""

        assert detect(_repeat("AAAAZZXX")) is None

    def test_a_national_phone_number_still_reports_phone(self) -> None:
        assert detect(_repeat("(555) 123-4567")) == "phone"

    def test_a_us_ssn_is_not_a_financial_identifier(self) -> None:
        assert detect(_repeat("123-45-6789")) is None

    def test_a_version_string_is_not_a_financial_identifier(self) -> None:
        """Reports `semver`, never `card_number`/`iban`/`bic`/`phone`."""

        assert detect(_repeat("10.14.2")) == "semver"


class TestBarcodes:
    """`isbn` and `ean`: both GS1 mod-10, `isbn` the `978`/`979` prefix subset of `ean`."""

    @pytest.mark.parametrize("value", ["9780306406157", "9791234567896"])
    def test_an_isbn13_is_detected(self, value: str) -> None:
        assert detect(_repeat(value)) == "isbn"

    @pytest.mark.parametrize("value", ["0306406152", "0132350882"])
    def test_an_isbn10_is_detected(self, value: str) -> None:
        assert detect(_repeat(value)) == "isbn"

    def test_an_isbn10_with_an_x_check_character_is_detected(self) -> None:
        assert detect(_repeat("080442957X")) == "isbn"

    def test_a_hyphenated_isbn_is_detected(self) -> None:
        """Hyphens are stripped before the check runs, the same precedent `card_number` set."""

        assert detect(_repeat("978-0-306-40615-7")) == "isbn"

    def test_an_isbn13_with_a_bad_check_digit_falls_through_to_numeric_string(self) -> None:
        assert detect(_repeat("9780306406158")) == "numeric_string"

    def test_an_isbn10_with_a_bad_check_character_falls_through_to_numeric_string(self) -> None:
        assert detect(_repeat("0306406151")) == "numeric_string"

    @pytest.mark.parametrize("value", ["4006381333931", "5901234123457"])
    def test_an_ean13_is_detected(self, value: str) -> None:
        assert detect(_repeat(value)) == "ean"

    def test_a_upca_is_detected(self) -> None:
        assert detect(_repeat("036000291452")) == "ean"

    def test_an_ean_with_a_bad_check_digit_falls_through_to_card_number(self) -> None:
        """This particular mutation happens to still be Luhn-valid at 13-digit length."""

        assert detect(_repeat("4006381333932")) == "card_number"

    def test_isbn_outranks_ean(self) -> None:
        """The `978`/`979` prefix subset must be claimed first or its rank never fires."""

        assert detect(_repeat("9780306406157")) == "isbn"

    def test_ean_that_also_passes_luhn_still_reports_ean(self) -> None:
        """The worked collision: a valid 13-digit EAN that also happens to be Luhn-valid."""

        assert detect(_repeat("5901234123457")) == "ean"

    def test_a_sequential_id_column_reports_no_pattern(self) -> None:
        """~10% GS1-valid by chance; 0.10 cannot clear the 0.95 threshold."""

        sample = [f"100000000000{i}" for i in range(30)]

        assert detect(sample) is None

    def test_a_us_ssn_is_not_a_barcode(self) -> None:
        assert detect(_repeat("123-45-6789")) is None


# isdigit()-true, isdecimal()-false: a superscript, a subscript, and a circled digit.
_HOSTILE_DIGITS = ("\u00b9", "\u2081", "\u2460")


class TestHostileCharacters:
    """isdigit()-true, isdecimal()-false codepoints must not reach `int()` and raise."""

    @pytest.mark.parametrize("digit", _HOSTILE_DIGITS)
    def test_a_hostile_digit_at_isbn10_length_does_not_raise(self, digit: str) -> None:
        assert detect(_repeat(f"030640615{digit}")) is None

    @pytest.mark.parametrize("digit", _HOSTILE_DIGITS)
    def test_a_hostile_digit_at_gtin13_length_does_not_raise(self, digit: str) -> None:
        assert detect(_repeat(f"400638133393{digit}")) is None


class TestVin:
    """17 chars, alphabet A-Z/0-9 minus I/O/Q, check digit at position 9 (49 C.F.R. 565.15)."""

    @pytest.mark.parametrize(
        "value",
        ["1HGCM82633A004352", "JH4TB2H26CC000000", "5YJ3E1EA7HF000337"],
    )
    def test_a_valid_vin_is_detected(self, value: str) -> None:
        assert detect(_repeat(value)) == "vin"

    def test_a_vin_with_an_x_check_digit_is_detected(self) -> None:
        assert detect(_repeat("1M8GDM9AXKP042788")) == "vin"

    def test_a_lowercase_vin_is_detected(self) -> None:
        """Case-folded before the check runs, the same as `iban`."""

        assert detect(_repeat("1hgcm82633a004352")) == "vin"

    def test_a_vin_with_a_bad_check_digit_is_not_detected(self) -> None:
        assert detect(_repeat("1HGCM82633A004353")) is None

    @pytest.mark.parametrize(
        "value",
        ["1HGCM82633I004352", "1HGCM82633O004352", "1HGCM82633Q004352"],
    )
    def test_i_o_and_q_are_not_matched(self, value: str) -> None:
        """The federal VIN regulation's Table III excludes these three letters."""

        assert detect(_repeat(value)) is None

    def test_a_short_all_uppercase_run_reports_no_pattern(self) -> None:
        """16 characters (one short of a VIN), uppercase and digits only, so it clears
        `base64`'s length floor and alphabet but fails its case-mixture floor.
        """

        assert detect(_repeat("1HGCM82633A00435")) is None


class TestImei:
    """Fifteen digits, Luhn-valid, first two digits an allocated Reporting Body Identifier."""

    @pytest.mark.parametrize("value", ["352099001761481", "490154203237518"])
    def test_an_imei_with_an_allocated_rbi_is_detected(self, value: str) -> None:
        assert detect(_repeat(value)) == "imei"

    def test_an_imei_inside_jcbs_iin_band_is_still_detected_as_imei(self) -> None:
        """`356938035643809` is the value a `card_number` IIN table would still claim."""

        assert detect(_repeat("356938035643809")) == "imei"

    def test_an_imei_column_and_an_amex_column_report_differently_in_the_same_run(self) -> None:
        """The pinning pair: settling the collision must not move `card_number`'s half of it."""

        assert detect(_repeat("352099001761481")) == "imei"
        assert detect(_repeat("378282246310005")) == "card_number"
        assert detect(_repeat("3782 822463 10005")) == "card_number"

    def test_an_unallocated_rbi_falls_through_to_card_number(self) -> None:
        """Luhn-valid at device length, but `60` was never allocated to a reporting body."""

        assert detect(_repeat("601100000000050")) == "card_number"

    def test_a_luhn_invalid_run_is_not_an_imei(self) -> None:
        assert detect(_repeat("352099001761482")) == "numeric_string"

    def test_a_bare_16_digit_pan_still_reports_card_number(self) -> None:
        assert detect(_repeat("4111111111111111")) == "card_number"


class TestHex:
    """Digests, colour codes and short SHAs; `hex` must not eat digit ids or postcodes."""

    @pytest.mark.parametrize(
        "value",
        [
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",  # sha256
            "5d41402abc4b2a76b9719d911017c592",  # md5
            "FF5733",
            "a3f9c2e",  # short git-style SHA
        ],
    )
    def test_a_hex_digest_or_colour_code_is_detected(self, value: str) -> None:
        assert detect(_repeat(value)) == "hex"

    def test_a_hash_prefixed_colour_code_is_detected(self) -> None:
        assert detect(_repeat("#FF5733")) == "hex"

    def test_a_compact_uuid_is_detected_as_hex(self) -> None:
        """SPEC 4.1.1's `uuid` excludes the compact form; `hex` claims it instead."""

        assert detect(_repeat("550e8400e29b41d4a716446655440000")) == "hex"

    def test_an_even_length_digit_id_is_not_hex(self) -> None:
        """No letter, so the value stays `numeric_string`."""

        assert detect(_repeat("12345678")) == "numeric_string"

    def test_a_compact_uk_postcode_is_not_hex(self) -> None:
        """`postal_code` outranks `hex`; the shape happens to be all-hex too."""

        assert detect(_repeat("EC1A1BB")) == "postal_code"

    def test_a_short_run_below_the_floor_is_not_hex(self) -> None:
        assert detect(_repeat("FF57")) is None


def _jwt(header: str, payload: str, signature: str = "sig") -> str:
    """Build a compact-serialization token from pre-encoded base64url segments."""

    return f"{header}.{payload}.{signature}"


class TestJwt:
    """RFC 7515 compact serialization; the header decode is the whole predicate."""

    _HS256_HEADER = "eyJhbGciOiJIUzI1NiJ9"  # {"alg":"HS256"}
    _NONE_HEADER = "eyJhbGciOiJub25lIn0"  # {"alg":"none"}
    _PAYLOAD = "eyJzdWIiOiIxIn0"  # {"sub":"1"}

    def test_a_signed_jwt_is_detected(self) -> None:
        assert detect(_repeat(_jwt(self._HS256_HEADER, self._PAYLOAD))) == "jwt"

    def test_an_unsecured_jwt_is_detected(self) -> None:
        """`alg: none` with an empty signature is RFC 7515's legal unsecured form."""

        assert detect(_repeat(_jwt(self._NONE_HEADER, self._PAYLOAD, ""))) == "jwt"

    def test_a_five_segment_jwe_is_not_detected(self) -> None:
        """A stated non-goal: JWE's compact serialization is a different shape.

        Segment lengths mirror a real RSA-OAEP/A128GCM JWE (RFC 7516 A.1), so a short final
        segment cannot make the value read as `filename` instead.
        """

        jwe = ".".join(
            [
                self._HS256_HEADER,
                "a" * 344,  # 2048-bit RSA-wrapped content-encryption key
                "b" * 16,  # 96-bit initialization vector
                "c" * 64,  # ciphertext
                "d" * 22,  # 128-bit authentication tag
            ],
        )

        assert detect(_repeat(jwe)) is None

    def test_a_dotted_non_jwt_token_is_not_detected(self) -> None:
        """A purely lexical three-dot-segment rule would claim this; the decode refuses it."""

        assert detect(_repeat("a.b.c")) == "filename"

    def test_a_header_without_alg_falls_through_to_filename(self) -> None:
        """A JSON header missing the required `alg` key declines `jwt`, same as `a.b.c`."""

        no_alg_header = "eyJ0eXAiOiJKV1QifQ"  # {"typ":"JWT"}

        assert detect(_repeat(_jwt(no_alg_header, self._PAYLOAD))) == "filename"

    def test_an_opaque_base64_token_still_reports_base64(self) -> None:
        assert detect(_repeat("YWJjZGVmZ2hpamtsbW5vcHFyc3R1dnd4eXo=")) == "base64"

    def test_a_sha256_digest_still_reports_hex(self) -> None:
        digest = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

        assert detect(_repeat(digest)) == "hex"


class TestMacAddress:
    @pytest.mark.parametrize(
        "value",
        ["00:1b:63:84:45:e6", "00-1B-63-84-45-E6"],
    )
    def test_a_separated_mac_is_detected(self, value: str) -> None:
        assert detect(_repeat(value)) == "mac_address"

    def test_an_all_digit_hyphenated_mac_outranks_phone(self) -> None:
        assert detect(_repeat("00-11-22-33-44-55")) == "mac_address"

    def test_a_separatorless_mac_is_hex_instead(self) -> None:
        """No separator, no evidence of an address - `hex` is the honest answer."""

        assert detect(_repeat("001b638445e6")) == "hex"

    def test_a_mixed_separator_run_is_not_a_mac_address(self) -> None:
        assert detect(_repeat("00:1b-63:84:45:e6")) is None

    def test_an_e164_phone_number_still_reports_phone(self) -> None:
        assert detect(_repeat("+15551234567")) == "phone"


class TestLatlon:
    @pytest.mark.parametrize(
        "value",
        ["51.5074,-0.1278", "-33.8688,151.2093", "51.5074, -0.1278"],
    )
    def test_a_coordinate_pair_is_detected(self, value: str) -> None:
        assert detect(_repeat(value)) == "latlon"

    def test_an_out_of_range_pair_is_not_detected(self) -> None:
        assert detect(_repeat("148.5,17.1")) is None

    def test_a_space_separated_pair_is_not_detected(self) -> None:
        """The comma is the evidence, on `phone`'s own separator-is-signal precedent."""

        assert detect(_repeat("51.5074 -0.1278")) is None

    def test_two_small_decimals_are_a_stated_gap(self) -> None:
        assert detect(_repeat("1.5,2.5")) == "latlon"


class TestUrn:
    @pytest.mark.parametrize(
        "value",
        ["urn:uuid:f81d4fae-7dec-11d0-a765-00a0c91e6bf6", "urn:isbn:0451450523"],
    )
    def test_a_urn_is_detected(self, value: str) -> None:
        assert detect(_repeat(value)) == "urn"

    def test_a_urn_with_a_slash_outranks_path(self) -> None:
        assert detect(_repeat("urn:example:weather/today")) == "urn"

    def test_a_canonical_uuid_still_reports_uuid(self) -> None:
        assert detect(_repeat("00000000-0000-7000-8000-000000000000")) == "uuid"

    def test_an_https_link_still_reports_url(self) -> None:
        assert detect(_repeat("https://example.com/a/very/long/path")) == "url"

    def test_a_real_path_still_reports_path(self) -> None:
        assert detect(_repeat("a/image.png")) == "path"


class TestIso8601Duration:
    @pytest.mark.parametrize(
        "value",
        ["P1Y2M3DT4H5M6S", "PT30M", "P4W"],
    )
    def test_a_valid_duration_is_detected(self, value: str) -> None:
        assert detect(_repeat(value)) == "iso8601_duration"

    def test_a_bare_p_is_not_a_duration(self) -> None:
        """At least one component is required; a bare designator carries none."""

        assert detect(_repeat("P")) is None

    def test_a_bare_pt_is_claimed_by_country_code_first(self) -> None:
        """`PT` (Portugal) ranks above `iso8601_duration`; not this pattern's row to own."""

        assert detect(_repeat("PT")) == "country_code"


class TestIso8601DateAndDatetime:
    """A date or datetime stored as text: two values, since they cast differently."""

    @pytest.mark.parametrize("value", ["2024-01-15", "2019-12-31"])
    def test_an_iso_date_is_detected(self, value: str) -> None:
        assert detect(_repeat(value)) == "iso8601_date"

    @pytest.mark.parametrize(
        "value",
        [
            "2024-01-15T09:30:00Z",
            "2024-01-15T09:30:00+02:00",
            "2024-01-15T09:30:00.123456Z",
            "2024-01-15T09:30:00",
            "2024-01-15 09:30:00",
        ],
    )
    def test_an_iso_datetime_is_detected(self, value: str) -> None:
        assert detect(_repeat(value)) == "iso8601_datetime"

    def test_the_basic_form_stays_numeric_string(self) -> None:
        """No hyphens, no evidence - indistinguishable from an eight-digit id."""

        assert detect(_repeat("20240115")) == "numeric_string"

    def test_a_slash_separated_date_stays_path(self) -> None:
        assert detect(_repeat("01/15/2024")) == "path"

    def test_a_week_date_is_not_detected(self) -> None:
        """ISO 8601's own week-date form (year, week, weekday) - unclaimed either way."""

        week, weekday = 1, 1
        week_date = f"2024-W{week:02d}-{weekday}"

        assert detect(_repeat(week_date)) is None

    def test_a_time_only_value_is_not_detected(self) -> None:
        assert detect(_repeat("09:30:00")) is None

    def test_a_year_only_value_stays_numeric_string(self) -> None:
        assert detect(_repeat("2024")) == "numeric_string"

    def test_an_impossible_calendar_date_is_not_detected(self) -> None:
        assert detect(_repeat("2024-02-31")) is None

    def test_a_us_ssn_is_not_detected(self) -> None:
        assert detect(_repeat("123-45-6789")) is None

    def test_a_mixed_date_and_datetime_column_reaches_no_verdict(self) -> None:
        """Different cast targets - a column genuinely mixing them has no single answer."""

        sample = ["2024-01-15"] * 18 + ["2024-01-15T09:30:00Z"] * 12

        assert detect(sample) is None


class TestIp:
    """One value covers both address families; a mixed column is what it exists for."""

    @pytest.mark.parametrize("value", ["192.0.2.1", "10.0.0.7"])
    def test_ipv4_is_detected(self, value: str) -> None:
        assert detect(_repeat(value)) == "ip"

    @pytest.mark.parametrize("value", ["2001:db8::1", "fe80::1"])
    def test_ipv6_is_detected(self, value: str) -> None:
        assert detect(_repeat(value)) == "ip"

    def test_an_ipv4_mapped_ipv6_address_is_detected(self) -> None:
        assert detect(_repeat("::ffff:192.0.2.1")) == "ip"

    def test_a_column_mixing_both_families_clears_the_threshold(self) -> None:
        """Two separate values could never fix a 70/30 split; one value covering both can."""

        sample = ["192.0.2.1"] * 7 + ["2001:db8::1"] * 3

        assert detect(sample) == "ip"

    @pytest.mark.parametrize("value", ["10.0.0.0/8", "192.168.1.0/24", "2001:db8::/32"])
    def test_a_cidr_block_is_not_an_address(self, value: str) -> None:
        """`ip` declines a network block; `path` declines it too."""

        assert detect(_repeat(value)) is None

    def test_a_host_and_port_is_not_an_address(self) -> None:
        assert detect(_repeat("192.0.2.1:8080")) is None

    def test_a_forwarded_for_chain_is_not_an_address(self) -> None:
        assert detect(_repeat("1.2.3.4, 5.6.7.8")) is None

    def test_a_bracketed_form_is_not_an_address(self) -> None:
        """Transport decoration, not the address itself."""

        assert detect(_repeat("[2001:db8::1]")) is None

    def test_a_zoned_form_is_not_an_address(self) -> None:
        """A zone index names an interface, not part of the address."""

        assert detect(_repeat("fe80::1%eth0")) is None

    def test_a_leading_zero_octet_is_not_detected(self) -> None:
        """A leading zero reads as octal to some resolvers, so the parser rejects it."""

        assert detect(_repeat("010.1.1.1")) is None


class TestSemver:
    """semver.org's grammar, plus the leading-`v` extension; outranks `path`/`filename`."""

    @pytest.mark.parametrize("value", ["1.2.3", "0.11.4", "10.14.2"])
    def test_a_release_version_is_detected(self, value: str) -> None:
        assert detect(_repeat(value)) == "semver"

    @pytest.mark.parametrize("value", ["2.0.0-rc.1", "1.2.3-0.3.7"])
    def test_a_numeric_prerelease_is_detected(self, value: str) -> None:
        assert detect(_repeat(value)) == "semver"

    @pytest.mark.parametrize("value", ["1.0.0-alpha.beta", "1.0.0+build.abc"])
    def test_an_alphabetic_prerelease_or_build_is_detected(self, value: str) -> None:
        """Without `semver` outranking `filename`, these would report `filename`."""

        assert detect(_repeat(value)) == "semver"

    def test_a_leading_v_is_detected(self) -> None:
        """semver.org's own grammar refuses this; git tags and dependency files store it."""

        assert detect(_repeat("v1.2.3")) == "semver"

    def test_a_two_part_version_stays_numeric_string(self) -> None:
        """`_NUMERIC_STRING_RE` already excludes a two-dot run; `semver` need not outrank it."""

        assert detect(_repeat("1.2")) == "numeric_string"
        assert detect(_repeat("10.14")) == "numeric_string"

    def test_a_hostname_still_reports_filename(self) -> None:
        """A stated gap: no rule separates a hostname from a filename."""

        assert detect(_repeat("example.com")) == "filename"
        assert detect(_repeat("api.internal.net")) == "filename"

    def test_a_filename_still_reports_filename(self) -> None:
        assert detect(_repeat("archive.tar")) == "filename"
        assert detect(_repeat("report.pdf")) == "filename"

    def test_an_extension_that_is_also_a_tld_still_reports_filename(self) -> None:
        """No list could do better - `.zip` and `.mov` are both delegated TLDs."""

        assert detect(_repeat("archive.zip")) == "filename"
        assert detect(_repeat("clip.mov")) == "filename"

    def test_slugs_report_no_pattern(self) -> None:
        """No slug pattern is defined: it would claim categorical columns."""

        sample = ["quarterly-report", "new-user-onboarding", "eu-vat-rules"] * 7

        assert detect(sample) is None

    def test_a_leading_zero_component_is_not_detected(self) -> None:
        assert detect(_repeat("01.2.3")) is None

    def test_a_four_part_dotted_run_reports_ip_not_semver(self) -> None:
        """`1.2.3.4` is a valid IPv4 address; `ip` outranks `semver` and claims it first."""

        assert detect(_repeat("1.2.3.4")) == "ip"


class TestPathDoesNotSubsumeDenserPatterns:
    """`path` is only "no whitespace and a separator", which denser encodings satisfy."""

    def test_long_base64_is_not_a_path(self) -> None:
        """The slash is in base64's alphabet, so a long token almost always carries one."""

        import base64 as b64
        import random

        random.seed(11)
        values = [
            b64.b64encode(bytes(random.randrange(256) for _ in range(192))).decode()
            for _ in range(40)
        ]

        assert detect(values) == "base64"

    def test_compact_json_carrying_a_url_is_not_a_path(self) -> None:
        assert detect(_repeat('{"url":"https://x/y","n":1}')) == "json"

    def test_a_real_path_still_wins_over_filename(self) -> None:
        assert detect(_repeat("a/image.png")) == "path"

    def test_an_all_digit_final_segment_is_still_a_path(self) -> None:
        """The network-block exclusion must not also reject an ordinary path."""

        assert detect(_repeat("report/42")) == "path"


class TestDetectWithEvidence:
    """The draw size and the winning pattern's tally, beside the same verdict `detect` returns."""

    def test_unanimous_match_reports_the_full_sample_as_matched(self) -> None:
        values = _repeat("US")
        match = detect_with_evidence(values)

        assert match.pattern == "country_code"
        assert match.sampled == match.matched == len(values)

    def test_a_partial_match_reports_the_share_that_agreed(self) -> None:
        values = ["person@example.com"] * 96 + ["not-an-email"] * 4

        match = detect_with_evidence(values)

        assert match.pattern == "email"
        assert match.sampled == 100
        assert match.matched == 96

    def test_no_pattern_clearing_the_threshold_reports_no_matched_tally(self) -> None:
        values = [f"free text value {i}" for i in range(TOLERANCE_MIN_SAMPLES)]

        match = detect_with_evidence(values)

        assert match.pattern is None
        assert match.sampled == TOLERANCE_MIN_SAMPLES
        assert match.matched == 0

    def test_an_empty_sample_reports_nothing_sampled(self) -> None:
        match = detect_with_evidence([])

        assert match.pattern is None
        assert match.sampled == 0


class TestNearMiss:
    """The best-scoring pattern below the verdict threshold (SPEC 4.1.3), floor at 50%."""

    def test_a_mixed_column_reports_the_dominant_pattern_and_its_share(self) -> None:
        values = _emails(70) + [f"free text value {i}" for i in range(30)]

        match = detect_with_evidence(values)

        assert match.pattern is None
        assert match.candidate == "email"
        assert match.candidate_share == pytest.approx(0.7)

    def test_a_column_matching_nothing_above_the_floor_reports_no_candidate(self) -> None:
        """Five shapes at even shares: the best of them clears neither the verdict nor the floor."""

        values = (
            _emails(16) + _uuids(16) + _media_types(16) + ["10.0.0.1"] * 16 + ["2026-01-01"] * 16
        )

        match = detect_with_evidence(values)

        assert match.pattern is None
        assert match.candidate is None
        assert match.candidate_share is None

    def test_exactly_at_the_floor_reports_a_candidate(self) -> None:
        values = _emails(50) + [f"free text value {i}" for i in range(50)]

        match = detect_with_evidence(values)

        assert match.candidate == "email"
        assert match.candidate_share == pytest.approx(NEAR_MISS_FLOOR)

    def test_just_below_the_floor_reports_no_candidate(self) -> None:
        values = _emails(49) + [f"free text value {i}" for i in range(51)]

        match = detect_with_evidence(values)

        assert match.candidate is None
        assert match.candidate_share is None

    def test_a_tie_at_the_bar_follows_the_priority_order(self) -> None:
        """`uuid` outranks `email` (SPEC 4.1.4); an even split resolves to it, not by scan order."""

        values = _uuids(50) + _emails(50)

        match = detect_with_evidence(values)

        assert match.candidate == "uuid"
        assert match.candidate_share == pytest.approx(0.5)

    def test_a_verdict_carries_no_candidate(self) -> None:
        """The near-miss and the verdict are mutually exclusive - a winner is never also one."""

        match = detect_with_evidence(_emails(100))

        assert match.pattern == "email"
        assert match.candidate is None
        assert match.candidate_share is None

    def test_an_empty_sample_reports_no_candidate(self) -> None:
        match = detect_with_evidence([])

        assert match.candidate is None
        assert match.candidate_share is None
        assert match.matched == 0
