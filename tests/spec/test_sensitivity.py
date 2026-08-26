"""Sensitivity detection: a recall-biased axis with the opposite error budget to looks_like."""

from __future__ import annotations

import importlib.resources
import json
from typing import get_args

import pytest

from dbprint.spec.looks_like import LooksLike
from dbprint.spec.sensitivity import (
    _CONTACT_SHAPES,
    _CREDENTIAL_SHAPES,
    _FINANCIAL_SHAPES,
    _GEOLOCATION_SHAPES,
    _ONLINE_IDENTIFIER_SHAPES,
    _WEAK_CREDENTIAL_SHAPES,
    Sensitivity,
    detect,
)


_PEOPLE = ["Ada Lovelace", "Grace Hopper", "Alan Turing", "Karen Sparck Jones"]
_COMPANIES = ["Acme Inc", "Globex Corporation", "Initech LLC", "Umbrella Group"]
_STREETS = ["221B Baker Street", "1600 Pennsylvania Avenue", "10 Downing Street"]


class TestEnumAgreement:
    """The Python `Literal` and the JSON Schema enum are hand-maintained twins."""

    def test_the_literal_and_the_schema_enum_carry_the_same_values(self) -> None:
        text = importlib.resources.files("dbprint.spec.v1").joinpath("statistics.schema.json")
        schema = json.loads(text.read_text())
        schema_values = set(schema["$defs"]["Sensitivity"]["enum"])

        assert set(get_args(Sensitivity)) == schema_values


class TestShapeSetGuard:
    """A shape set naming a dead `looks_like` value matches nothing, yet looks covered."""

    def test_every_shape_set_member_is_a_live_looks_like_value(self) -> None:
        live = set(get_args(LooksLike))

        for name, shapes in (
            ("_CONTACT_SHAPES", _CONTACT_SHAPES),
            ("_FINANCIAL_SHAPES", _FINANCIAL_SHAPES),
            ("_ONLINE_IDENTIFIER_SHAPES", _ONLINE_IDENTIFIER_SHAPES),
            ("_GEOLOCATION_SHAPES", _GEOLOCATION_SHAPES),
            ("_CREDENTIAL_SHAPES", _CREDENTIAL_SHAPES),
            ("_WEAK_CREDENTIAL_SHAPES", _WEAK_CREDENTIAL_SHAPES),
        ):
            assert shapes <= live, f"{name} claims a shape absent from LooksLike: {shapes - live}"


class TestPersonalName:
    @pytest.mark.parametrize(
        "column",
        ["first_name", "last_name", "surname", "full_name", "customer_name", "recipient"],
    )
    def test_an_unambiguous_column_name_is_enough(self, column: str) -> None:
        """No value agreement required: initials and placeholders are still a name column."""

        assert detect(column, ["X", "?", "--"]) == "personal_name"

    def test_camel_case_and_hyphens_normalize(self) -> None:
        assert detect("firstName", []) == "personal_name"
        assert detect("first-name", []) == "personal_name"

    def test_an_ambiguous_column_of_people_is_flagged(self) -> None:
        """Recall bias resolves `name` toward flagging when the values agree."""

        assert detect("name", _PEOPLE) == "personal_name"

    def test_an_ambiguous_column_of_companies_is_not(self) -> None:
        """`vendors.name` and `curator.name` are separable only by their values."""

        assert detect("name", _COMPANIES) is None

    def test_an_unrelated_column_is_not_flagged(self) -> None:
        assert detect("widget_count", _PEOPLE) is None

    @pytest.mark.parametrize(
        ("column", "values"),
        [
            ("customer_first_name", ["Jane Doe", "John Smith", "Ada Lovelace"]),
            ("billing_first_name", ["J.", "A."]),
            ("user_last_name", ["Lovelace"]),
        ],
    )
    def test_a_prefixed_or_suffixed_strong_token_is_still_unambiguous(
        self,
        column: str,
        values: list[str],
    ) -> None:
        """A strong token stays strong under a qualifying head - no value agreement needed."""

        assert detect(column, values) == "personal_name"

    @pytest.mark.parametrize("column", ["company_name", "product_name"])
    def test_a_disqualified_head_is_not_flagged_even_with_people_shaped_values(
        self,
        column: str,
    ) -> None:
        """The disqualifying head vetoes outright - it never reaches value corroboration."""

        assert detect(column, _PEOPLE) is None

    def test_an_unlisted_head_still_reaches_value_corroboration(self) -> None:
        """No head list required: an unrecognized qualifier costs nothing per SPEC 4.4.3."""

        assert detect("applicant_name", _PEOPLE) == "personal_name"
        assert detect("team_name", ["prod-eu-1", "prod-us-2"]) is None

    def test_a_glued_token_is_not_decomposed(self) -> None:
        """A stated gap: no separator means no token boundary to anchor on."""

        assert detect("firstname", _PEOPLE) is None

    @pytest.mark.parametrize("column", ["username", "filename", "hostname"])
    def test_the_three_names_the_tail_anchor_exists_to_refuse_stay_unmatched(
        self,
        column: str,
    ) -> None:
        """`_matches`'s tail anchor keeps these three off the name axis with no deny-list."""

        assert detect(column, _PEOPLE) is None


class TestPostalAddress:
    def test_a_street_column_is_flagged(self) -> None:
        assert detect("street_address", _STREETS) == "postal_address"

    def test_an_address_column_with_no_sample_is_still_flagged(self) -> None:
        """Recall bias: an unsampled address column is not evidence of safety.

        `_is_postal_address` reads an empty sample as agreement, where `_share([])` scores 0.0.
        """

        assert detect("billing_address", []) == "postal_address"

    def test_a_prefixed_address_token_is_flagged(self) -> None:
        assert detect("customer_address", _STREETS) == "postal_address"

    @pytest.mark.parametrize("column", ["email_address", "ip_address", "mac_address"])
    def test_an_address_shaped_word_that_is_not_one_is_not_flagged(self, column: str) -> None:
        """The disqualifying head keeps the tail anchor off words that only end in `address`."""

        assert detect(column, _STREETS) != "postal_address"

    def test_a_head_anchored_qualifier_is_a_stated_gap(self) -> None:
        """`address_line_1`'s qualifying word is the head, not the tail - out of reach."""

        assert detect("address_line_1", _STREETS) is None


class TestGeolocation:
    @pytest.mark.parametrize(
        "column",
        ["latitude", "longitude", "lat", "lon", "lng", "coordinates", "geo_location", "geohash"],
    )
    def test_an_unambiguous_column_name_is_enough(self, column: str) -> None:
        """No sample is drawn for `numeric`, so name evidence alone must be enough."""

        assert detect(column, []) == "geolocation"

    def test_a_latlon_shaped_value_carries_it_whatever_the_name_is(self) -> None:
        assert detect("payload", ["51.5074,-0.1278"], "latlon") == "geolocation"

    def test_a_city_column_is_not_flagged(self) -> None:
        """A `redact` rule here would silently empty the enumeration a print publishes."""

        assert detect("city", ["London", "Vienna"]) is None

    def test_a_postcode_column_is_not_flagged(self) -> None:
        """Postcode-adjacent columns belong to `postal_address`, not this category."""

        assert detect("postcode", ["811 01"]) is None

    def test_an_ordinary_numeric_column_is_not_flagged(self) -> None:
        assert detect("total", ["55000"]) is None


class TestDateOfBirth:
    @pytest.mark.parametrize(
        "column",
        [
            "date_of_birth",
            "dob",
            "birth_date",
            "birthdate",
            "birthday",
            "born_on",
            "born_at",
            "customer_date_of_birth",
            "patient_date_of_birth",
            "user_dob",
            "employee_dob",
        ],
    )
    def test_an_unambiguous_column_name_is_enough(self, column: str) -> None:
        """No value agreement or sample required - the whole vocabulary is unambiguous."""

        assert detect(column, []) == "date_of_birth"

    def test_camel_case_and_hyphens_normalize(self) -> None:
        assert detect("dateOfBirth", []) == "date_of_birth"
        assert detect("date-of-birth", []) == "date_of_birth"

    def test_an_age_column_is_not_flagged(self) -> None:
        """`age` is numeric, coarser, and as often a cache/account/file age."""

        assert detect("age", ["34", "51", "22"]) is None

    def test_an_event_timestamp_column_is_not_flagged(self) -> None:
        assert detect("created_at", []) is None
        assert detect("updated_at", []) is None


class TestNationalId:
    @pytest.mark.parametrize(
        "column",
        [
            "ssn",
            "social_security_number",
            "national_id",
            "national_insurance_number",
            "nino",
            "tax_id",
            "vat_number",
            "passport_number",
            "passport_no",
            "drivers_license",
            "driving_licence",
        ],
    )
    def test_an_unambiguous_column_name_is_enough(self, column: str) -> None:
        """No value agreement required, the same treatment `first_name` gets."""

        assert detect(column, ["X", "?", "--"]) == "national_id"

    def test_a_dashed_ssn_is_flagged_by_name_alone(self) -> None:
        assert detect("ssn", ["123-45-6789"]) == "national_id"

    def test_a_bare_ssn_is_flagged_by_name_alone(self) -> None:
        """The strong tier needs no shape corroboration - `looks_like` stays absent either way."""

        assert detect("ssn", ["123456789"]) == "national_id"

    def test_an_internal_id_number_is_not_flagged(self) -> None:
        """Weak token, values dissent: a sequential internal id has no SSN shape."""

        values = [str(i) for i in range(1, 100000)]

        assert detect("id_number", values[:50]) is None

    def test_a_corroborated_id_number_is_flagged(self) -> None:
        """Weak token, values agree: dashed SSN-shaped values corroborate the generic name."""

        assert detect("id_number", ["123-45-6789"] * 10) == "national_id"

    @pytest.mark.parametrize("column", ["sin", "tin"])
    def test_ordinary_words_need_corroboration_too(self, column: str) -> None:
        """`sin`/`tin` are also a trigonometric function and a metal - weak tier only."""

        assert detect(column, ["not-an-ssn"] * 10) is None
        assert detect(column, ["123-45-6789"] * 10) == "national_id"

    @pytest.mark.parametrize("area", ["000", "666", "900", "999"])
    def test_an_ssn_shaped_invalid_area_does_not_corroborate(self, area: str) -> None:
        """The SSA never issues these area codes; the shape check excludes them."""

        assert detect("id_number", [f"{area}-45-6789"] * 10) is None

    def test_an_ssn_shaped_invalid_group_does_not_corroborate(self) -> None:
        assert detect("id_number", ["123-00-6789"] * 10) is None

    def test_an_ssn_shaped_invalid_serial_does_not_corroborate(self) -> None:
        assert detect("id_number", ["123-45-0000"] * 10) is None

    def test_a_zip4_is_not_flagged(self) -> None:
        """A hyphenated all-digit code that is not a government identifier."""

        assert detect("zip4", ["94103-1234"]) is None


class TestFinancialAccount:
    @pytest.mark.parametrize(
        "column",
        [
            "iban",
            "bank_account_number",
            "bank_account",
            "card_number",
            "card_no",
            "credit_card",
            "debit_card",
            "cc_number",
            "cvv",
            "cvc",
            "card_security_code",
        ],
    )
    def test_an_unambiguous_column_name_is_enough(self, column: str) -> None:
        """No value agreement, or even a sample, required - the strong tier's whole point."""

        assert detect(column, []) == "financial_account"

    def test_an_iban_shaped_value_carries_it_whatever_the_name_is(self) -> None:
        """A mod-97 check is corroboration on its own, unlike any heuristic category."""

        assert detect("ref", ["SK3112000000198742637541"], "iban") == "financial_account"

    def test_a_pan_shaped_value_carries_it_whatever_the_name_is(self) -> None:
        assert detect("ref", ["4111111111111111"], "card_number") == "financial_account"

    def test_a_numeric_card_column_is_flagged_by_name_alone(self) -> None:
        """No sample is drawn for `numeric`, so the name is the only evidence available."""

        assert detect("card_number", []) == "financial_account"

    def test_an_internal_account_number_is_not_flagged(self) -> None:
        """Generic name, no corroborating shape - the weak tier's negative."""

        assert detect("account_number", ["ACCT-100042"], None) is None

    def test_a_corroborated_account_number_is_flagged(self) -> None:
        """Generic name, an IBAN-shaped value - the weak tier's positive."""

        assert detect("account_number", ["SK3112000000198742637541"], "iban") == "financial_account"

    def test_a_numeric_account_number_is_not_flagged(self) -> None:
        """No sample on `numeric`, so the weak tier - which needs the shape - cannot fire."""

        assert detect("account_number", [], None) is None

    @pytest.mark.parametrize("column", ["bic", "swift_code", "sort_code", "routing_number"])
    def test_institution_identifiers_are_refused(self, column: str) -> None:
        """These name a bank, not an account - refused rather than unconsidered."""

        assert detect(column, ["SEEDGB2L"], "bic") is None

    def test_a_tokenised_card_column_is_not_flagged(self) -> None:
        """No name token and no shape - `1111` reports no `looks_like` on its own."""

        assert detect("card_last4", ["1111"], None) is None


class TestCredential:
    @pytest.mark.parametrize(
        "column",
        [
            "password",
            "password_hash",
            "api_key",
            "secret_key",
            "access_token",
            "refresh_token",
            "session_token",
            "private_key",
            "client_secret",
        ],
    )
    def test_an_unambiguous_column_name_is_enough(self, column: str) -> None:
        """No value inspection or sample required - the strong tier's whole point."""

        assert detect(column, []) == "credential"

    def test_the_jwt_shape_carries_it_whatever_the_name_is(self) -> None:
        assert detect("payload", ["a.b.c"], "jwt") == "credential"

    def test_a_weak_name_corroborated_by_jwt_is_flagged(self) -> None:
        assert detect("token", [], "jwt") == "credential"

    def test_a_weak_name_corroborated_by_hex_is_flagged(self) -> None:
        assert detect("key", [], "hex") == "credential"

    def test_a_settings_table_key_column_is_not_flagged(self) -> None:
        """Weak name, no corroborating shape - a settings table's own key is not a secret."""

        assert detect("key", ["theme", "locale", "timezone"], None) is None

    def test_a_checksum_column_is_not_flagged_by_hex_alone(self) -> None:
        """`hex` corroborates a weak name; it does not create one on its own."""

        assert detect("checksum", [], "hex") is None

    def test_a_compound_weak_token_is_not_reached_by_containment(self) -> None:
        """The weak tier stays whole-string: containment would flag every `*_hash` checksum."""

        assert detect("content_hash", [], "hex") is None


class TestHealth:
    @pytest.mark.parametrize(
        "column",
        [
            "diagnosis",
            "diagnoses",
            "diagnosis_code",
            "diagnosis_notes",
            "icd10",
            "icd10_code",
            "blood_type",
            "blood_group",
            "medical_condition",
            "medication",
            "prescription",
            "allergy",
            "allergies",
            "disability",
            "disability_status",
        ],
    )
    def test_an_unambiguous_column_name_is_enough(self, column: str) -> None:
        assert detect(column, []) == "health"

    def test_a_prose_notes_column_still_carries_it(self) -> None:
        """No value list exists to withhold; the flag is not conditional on there being one."""

        assert detect("diagnosis_notes", ["patient reports mild symptoms"], "prose") == "health"

    @pytest.mark.parametrize("column", ["condition", "treatment", "procedure", "status"])
    def test_an_ordinary_operational_column_is_not_flagged(self, column: str) -> None:
        """These are a rules predicate, an experiment arm, a stored procedure, a CI run."""

        assert detect(column, ["A", "B"]) is None


class TestDemographic:
    @pytest.mark.parametrize(
        "column",
        [
            "ethnicity",
            "ethnic_group",
            "race",
            "religion",
            "religious_affiliation",
            "sexual_orientation",
            "gender_identity",
            "political_affiliation",
            "union_membership",
        ],
    )
    def test_an_unambiguous_column_name_is_enough(self, column: str) -> None:
        assert detect(column, []) == "demographic"

    @pytest.mark.parametrize("column", ["gender", "sex", "nationality", "country", "language"])
    def test_a_common_breakdown_column_is_not_flagged(self, column: str) -> None:
        """The breakdown an analytics print is most often written to support."""

        assert detect(column, ["a", "b"]) is None

    def test_a_health_only_rule_leaves_demographic_columns_reachable(self) -> None:
        """The split is load-bearing: each category is independently targetable."""

        assert detect("blood_type", []) == "health"
        assert detect("ethnicity", []) == "demographic"


class TestEmployment:
    @pytest.mark.parametrize(
        "column",
        [
            "salary",
            "annual_salary",
            "base_salary",
            "wage",
            "hourly_rate",
            "pay",
            "base_pay",
            "gross_pay",
            "net_pay",
            "compensation",
            "bonus",
            "commission",
        ],
    )
    def test_an_unambiguous_column_name_is_enough(self, column: str) -> None:
        """No sample is drawn for `numeric`, so name evidence alone must be enough."""

        assert detect(column, []) == "employment"

    def test_a_text_typed_salary_column_is_still_flagged(self) -> None:
        """The classification does not matter to this axis - only the name does."""

        assert detect("annual_salary", ["50000", "62500"]) == "employment"

    @pytest.mark.parametrize(
        "column",
        ["performance_rating", "termination_reason", "manager_notes", "job_title", "department"],
    )
    def test_an_adjacent_hr_column_is_not_flagged(self, column: str) -> None:
        """An HR dashboard groups by these; a false positive would empty them silently."""

        assert detect(column, ["a", "b"]) is None

    @pytest.mark.parametrize("column", ["total", "amount", "price"])
    def test_ordinary_money_is_not_flagged(self, column: str) -> None:
        assert detect(column, ["99.99"]) is None


class TestContact:
    def test_the_email_shape_carries_it(self) -> None:
        assert detect("primary", ["a@b.com"], "email") == "contact"

    def test_the_phone_shape_carries_it(self) -> None:
        """An unhelpful column name is no obstacle - the shape alone is evidence."""

        assert detect("contact_field", ["+1 (555) 123-4567"], "phone") == "contact"

    def test_a_contact_column_name_carries_it(self) -> None:
        assert detect("phone_number", ["555 0100"]) == "contact"

    @pytest.mark.parametrize("column", ["msisdn", "cell", "tel"])
    def test_an_industry_name_carries_it(self, column: str) -> None:
        assert detect(column, ["447700900123"]) == "contact"

    def test_a_bare_number_column_is_not_flagged_by_name_alone(self) -> None:
        """`number` is not a contact token; a phone table's own PK stays unflagged."""

        assert detect("number", ["5551234567"]) is None

    def test_an_order_number_is_not_flagged(self) -> None:
        """The false-positive guard: an order id is a digit run of similar length."""

        assert detect("number", ["1000042"]) is None

    def test_a_prefixed_contact_token_with_no_shape_to_fall_back_on_is_flagged(self) -> None:
        """Bare digits carry no `looks_like`; the name alone is what reaches this column."""

        assert detect("work_phone", ["5551234567"]) == "contact"

    @pytest.mark.parametrize("column", ["cell_count", "mobile_app_version"])
    def test_a_contact_token_as_a_leading_rather_than_trailing_run_is_not_flagged(
        self,
        column: str,
    ) -> None:
        """The tail anchor requires the token to be trailing - a leading `cell`/`mobile` is not."""

        assert detect(column, ["4211"]) is None

    def test_a_name_outranks_a_contact_shape(self) -> None:
        """One field, so the more specific category wins."""

        assert detect("first_name", ["a@b.com"], "email") == "personal_name"

    def test_national_id_outranks_a_contact_shape(self) -> None:
        """The chain checks most-specific first; `national_id` sits above `contact`."""

        assert detect("ssn", ["a@b.com"], "email") == "national_id"

    def test_financial_account_outranks_a_contact_shape(self) -> None:
        assert detect("card_number", ["a@b.com"], "email") == "financial_account"


class TestOnlineIdentifier:
    @pytest.mark.parametrize(
        "column",
        [
            "ip_address",
            "ipaddr",
            "client_ip",
            "remote_addr",
            "mac_address",
            "device_id",
            "advertising_id",
            "idfa",
            "session_id",
            "cookie_id",
            "visitor_id",
        ],
    )
    def test_an_unambiguous_column_name_is_enough(self, column: str) -> None:
        assert detect(column, []) == "online_identifier"

    def test_the_ip_shape_carries_it_whatever_the_name_is(self) -> None:
        assert detect("remote", ["203.0.113.42"], "ip") == "online_identifier"

    def test_the_mac_address_shape_carries_it_whatever_the_name_is(self) -> None:
        assert detect("hw_addr", ["02:00:00:00:00:01"], "mac_address") == "online_identifier"

    def test_an_internal_key_is_not_flagged(self) -> None:
        """The decisive exclusion: flagging these puts a rule over every foreign key."""

        for column in ("user_id", "customer_id", "account_id"):
            assert detect(column, ["1", "2"]) is None

    def test_a_uuid_column_is_not_flagged_by_shape(self) -> None:
        """`uuid` is the commonest primary key in any schema and must not become shape evidence."""

        assert detect("id", ["a" * 20], "uuid") is None

    def test_a_username_column_is_not_flagged(self) -> None:
        """Settled: a handle identifies an account, not a device or network endpoint."""

        assert detect("username", ["jane.doe"]) is None

    def test_a_session_token_is_not_flagged_here(self) -> None:
        """A session token is `credential` first; this rules out `online_identifier`, not None."""

        assert detect("session_token", []) != "online_identifier"

    def test_a_contact_shape_outranks_it(self) -> None:
        """An email is also an online identifier; the more specific category wins."""

        assert detect("primary", ["a@b.com"], "email") == "contact"


class TestDetectionChain:
    """`detect()` checks most-specific first; the order is asserted, not assumed."""

    def test_the_chain_is_name_address_national_id_financial_account_contact(self) -> None:
        assert detect("first_name", []) == "personal_name"
        assert detect("street_address", ["221B Baker Street"]) == "postal_address"
        assert detect("latitude", []) == "geolocation"
        assert detect("dob", []) == "date_of_birth"
        assert detect("ssn", []) == "national_id"
        assert detect("card_number", []) == "financial_account"
        assert detect("api_key", []) == "credential"
        assert detect("salary", []) == "employment"
        assert detect("email", ["a@b.com"]) == "contact"
        assert detect("device_id", []) == "online_identifier"


class TestTailAnchorReachesEveryTierButWeakCredentials:
    """The widening lands on all fifteen token sets except `_WEAK_CREDENTIAL_TOKENS`.

    Only the weak tier, corroborated by shape rather than value, is held out.
    """

    @pytest.mark.parametrize(
        ("column", "expected"),
        [
            ("customer_ssn", "national_id"),
            ("primary_iban", "financial_account"),
            ("admin_password", "credential"),
            ("patient_diagnosis", "health"),
            ("employee_ethnicity", "demographic"),
            ("employee_salary", "employment"),
            ("store_latitude", "geolocation"),
            ("employee_date_of_birth", "date_of_birth"),
            ("user_ip", "online_identifier"),
        ],
    )
    def test_a_previously_unreachable_compound_now_reports_its_category(
        self,
        column: str,
        expected: str,
    ) -> None:
        assert detect(column, []) == expected


class TestNothingDetected:
    @pytest.mark.parametrize(
        "column",
        ["total_amount", "created_at", "status", "row_count", "id", "user_id", "order_id"],
    )
    def test_an_ordinary_column_reports_nothing(self, column: str) -> None:
        assert detect(column, ["1", "2", "3"]) is None
