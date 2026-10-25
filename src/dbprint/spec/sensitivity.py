"""Sensitivity detection per SPEC v1, section 4.4.

`detect(column_name, values, assigned=...)` returns the category of must-not-leave-the-database
data a column carries, or None. Recall-biased where `looks_like` is precision-biased: an
over-flag costs one over-masked column, a miss leaks a name or a live credential into a git
repository. Absence is "not detected", never "safe to publish"; dbprint is no compliance tool.

Every token set below is matched by `_matches` as a trailing, separator-delimited token run of
the normalized name; `_COMPOUND_CREDENTIAL_TOKENS` needs a head before the run, and
`_WEAK_CREDENTIAL_TOKENS` alone is matched whole-string. A few sets also
carry a disqualifying-head list vetoing a match whose preceding token names something that is
not personal data (`company_name`); those lists hold English qualifier nouns, never names of
people - the given-names dictionary SPEC 4.4.3 bans. An unlisted head neither vetoes nor
qualifies: SPEC 4.4.3 forbids demanding value agreement for an unambiguous column name. Two
stated gaps: `address_line_1` puts its qualifying word at the head, beyond the tail anchor's
reach, and a word naming both a sensitive and an ordinary thing (`race`, `mobile`) is
disambiguated by no list here.

Pure: no I/O, no state.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from typing import Literal

from .looks_like import EMAIL_ADDRESS, LooksLike
from .secret_formats import holds_secret


Sensitivity = Literal[
    "personal_name",
    "postal_address",
    "geolocation",
    "date_of_birth",
    "national_id",
    "financial_account",
    "credential",
    "health",
    "demographic",
    "employment",
    "contact",
    "online_identifier",
]

# Corroboration thresholds, per detector rather than shared (SPEC 4.4.4). Set low: a name
# column with `-- unknown --` sentinels in a tenth of its rows is still a name column.
PERSONAL_NAME_VALUE_THRESHOLD = 0.5
POSTAL_ADDRESS_VALUE_THRESHOLD = 0.5
NATIONAL_ID_VALUE_THRESHOLD = 0.5
CONTACT_SHAPE_THRESHOLD = 0.5
FINANCIAL_ACCOUNT_SHAPE_THRESHOLD = 0.5
ONLINE_IDENTIFIER_SHAPE_THRESHOLD = 0.5
GEOLOCATION_SHAPE_THRESHOLD = 0.5
CREDENTIAL_SHAPE_THRESHOLD = 0.5

# Column names that identify a person's name on their own; no corroboration (SPEC 4.4.3).
_STRONG_NAME_TOKENS = frozenset(
    {
        "first_name",
        "last_name",
        "middle_name",
        "given_name",
        "family_name",
        "sur_name",
        "surname",
        "forename",
        "maiden_name",
        "full_name",
        "customer_name",
        "contact_name",
        "employee_name",
        "patient_name",
        "author_name",
        "recipient",
        "recipient_name",
    },
)

# Column names that MIGHT be a person's name, needing the values to agree: `vendors.name`
# holding companies and `people.name` holding people are indistinguishable by name alone.
_WEAK_NAME_TOKENS = frozenset({"name", "display_name", "owner", "author", "contact"})

# The tokens naming the thing itself; every other name token names a person attached to it.
_THING_NAME_TOKENS = frozenset({"name", "display_name", "full_name"})

# Heads naming an organisation, place, label or system object - never a person - veto a thing's
# name; a bare `name` in a table one of them names (singular or plural) is vetoed too.
_NAME_DISQUALIFIED_HEADS = frozenset(
    {
        "account",
        "branch",
        "brand",
        "building",
        "campaign",
        "category",
        "channel",
        "city",
        "cluster",
        "column",
        "company",
        "country",
        "domain",
        "event",
        "field",
        "file",
        "group",
        "host",
        "list",
        "location",
        "merchant",
        "metric",
        "office",
        "organisation",
        "organization",
        "partner",
        "plan",
        "product",
        "program",
        "project",
        "region",
        "report",
        "reseller",
        "segment",
        "site",
        "stage",
        "status",
        "store",
        "supplier",
        "survey",
        "table",
        "team",
        "template",
        "tier",
        "type",
        "vendor",
        "venue",
        "workflow",
    },
)

# Column names that identify a state-issued identifier on their own; nothing competes.
_STRONG_NATIONAL_ID_TOKENS = frozenset(
    {
        "ssn",
        "social_security_number",
        "national_id",
        "national_insurance_number",
        "nino",
        "passport_number",
        "passport_no",
        "drivers_license",
        "driving_licence",
    },
)

# Tax identifiers are issued to organisations as often as to people, so a head naming the
# organisation that holds one vetoes the match.
_TAX_ID_TOKENS = frozenset({"tax_id", "vat_number"})
_TAX_ID_DISQUALIFIED_HEADS = frozenset(
    {
        "business",
        "company",
        "corporate",
        "customer",
        "merchant",
        "organisation",
        "organization",
        "partner",
        "reseller",
        "seller",
        "supplier",
        "vendor",
    },
)

# Column names that MIGHT hold a government identifier: `sin`/`tin` are also ordinary English
# words and `id_number` is as often an internal customer id, so all three need value
# agreement.
_WEAK_NATIONAL_ID_TOKENS = frozenset({"sin", "tin", "id_number"})

# Column names that identify a bank or card account on their own. Institution identifiers
# (`bic`, `swift_code`, `sort_code`, `routing_number`) are absent - they name a bank, not an
# account, and `sort_code` also collides with display-ordering columns.
_STRONG_FINANCIAL_ACCOUNT_TOKENS = frozenset(
    {
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
    },
)

# Column names that identify a secret on their own; no value agreement needed.
_STRONG_CREDENTIAL_TOKENS = frozenset(
    {
        "password",
        "passwd",
        "pwd",
        "password_hash",
        "password_digest",
        "password_salt",
        "api_key",
        "secret_key",
        "secret_access_key",
        "access_token",
        "refresh_token",
        "session_token",
        "private_key",
        "client_secret",
    },
)

# Column names that MIGHT be a secret - a settings or cache `key`, a content `hash` are the
# ordinary meanings competing with them. Corroborated by shape, never by a per-value
# threshold: `hex` pairs only with these, since an unconditional `hex` would catch checksums.
_WEAK_CREDENTIAL_TOKENS = frozenset({"key", "token", "secret", "hash"})

# `token` and `secret` ending a longer name (`remember_token`) name a secret; `key`, `hash` do not.
_COMPOUND_CREDENTIAL_TOKENS = frozenset({"token", "secret"})

# `looks_like` values that are themselves credentials, whatever the column name: RFC 7515's
# header decode already rules out an ordinary dotted token (SPEC 4.1.1).
_CREDENTIAL_SHAPES = frozenset({"jwt"})

# `looks_like` values that corroborate a weak credential name, at the shape share (SPEC 4.4.4).
_WEAK_CREDENTIAL_SHAPES = frozenset({"jwt", "hex"})

# Column names that identify a coordinate, coordinate pair or geohash on their own. `city`,
# `region`, `postcode` and `zip` are absent - redacting them would empty the enumeration a
# print exists to publish, and a postcode belongs to `postal_address` (SPEC 4.4.1).
_GEOLOCATION_TOKENS = frozenset(
    {
        "latitude",
        "longitude",
        "lat",
        "lon",
        "lng",
        "geo_lat",
        "geo_lng",
        "coordinates",
        "geo_location",
        "geolocation",
        "geohash",
        "gps",
    },
)

# `looks_like` values that are themselves coordinates.
_GEOLOCATION_SHAPES = frozenset({"latlon"})

# Column names that identify a birth date on their own, no sample needed. `age` is absent:
# it is `numeric` rather than `temporal`, publishes a coarser range, and means cache or file
# age at least as often as a person's (SPEC 4.4.1). Compounds like `customer_date_of_birth`
# need no entry - `_matches` anchors on the tail.
_DATE_OF_BIRTH_TOKENS = frozenset(
    {
        "date_of_birth",
        "dob",
        "birth_date",
        "birthdate",
        "birthday",
        "born_on",
        "born_at",
    },
)

# Column names that identify clinical data on their own. `condition`, `treatment`,
# `procedure`/`procedure_code`, `test_result`, `status` and `weight`/`height`/`bmi` are
# absent - each is a rules predicate, an experiment arm, a stored procedure, a CI run or
# freight at least as often as a patient's (SPEC 4.4.1).
_HEALTH_TOKENS = frozenset(
    {
        "diagnosis",
        "diagnoses",
        "diagnosis_code",
        "diagnosis_notes",
        "icd10",
        "icd10_code",
        "icd9_code",
        "icd_code",
        "blood_type",
        "blood_group",
        "medical_condition",
        "health_condition",
        "medication",
        "prescription",
        "allergy",
        "allergies",
        "disability",
        "disability_status",
    },
)

# Column names that identify a demographic category needing extra care. Bare `gender`/`sex`,
# `nationality`/`country` (join keys) and `marital_status`/`language` (CRM fields) are absent:
# each is what an analytics print is most often written to break down by (SPEC 4.4.1).
_DEMOGRAPHIC_TOKENS = frozenset(
    {
        "ethnicity",
        "ethnic_group",
        "race",
        "religion",
        "religious_affiliation",
        "sexual_orientation",
        "gender_identity",
        "political_affiliation",
        "political_party",
        "union_membership",
        "trade_union",
    },
)

# Column names that identify compensation on their own: no sample is drawn for `numeric`,
# so the name is the only evidence. `performance_rating`, `termination_reason`,
# `manager_notes`, `job_title` and `department` are absent - each is an ordinary analytics
# column an HR dashboard groups by (SPEC 4.4.1).
_EMPLOYMENT_TOKENS = frozenset(
    {
        "salary",
        "annual_salary",
        "base_salary",
        "salary_amount",
        "wage",
        "hourly_rate",
        "hourly_wage",
        "pay_rate",
        "base_pay",
        "gross_pay",
        "net_pay",
        "compensation",
        "bonus",
        "commission",
    },
)

# Heads naming money that is not a person's pay - a partner's commission, a refund paid out.
_EMPLOYMENT_DISQUALIFIED_HEADS = frozenset(
    {
        "affiliate",
        "agency",
        "company",
        "customer",
        "event",
        "merchant",
        "partner",
        "platform",
        "refund",
        "reseller",
        "supplier",
        "total",
        "vendor",
    },
)

_ADDRESS_TOKENS = frozenset(
    {
        "address",
        "street",
        "street_address",
        "address_line1",
        "address_line2",
        "address1",
        "address2",
        "billing_address",
        "shipping_address",
        "mailing_address",
        "home_address",
    },
)

# Heads that name an address-shaped word which is not a postal address.
_ADDRESS_DISQUALIFIED_HEADS = frozenset({"email", "ip", "mac"})

_CONTACT_TOKENS = frozenset(
    {
        "email",
        "email_address",
        "e_mail",
        "phone",
        "phone_number",
        "telephone",
        "mobile",
        "mobile_number",
        "fax",
        "msisdn",
        "cell",
        "cell_phone",
        "mobile_phone",
        "tel",
        "telephone_number",
        "contact_number",
        "phone_no",
    },
)

# Column names that identify a network address, device or session identifier on their own.
# `session_token` is absent because a live session token is a bearer credential first, so it
# reports `credential` and the two sets must not collide. Internal keys (`user_id`/
# `customer_id`/`account_id`), `username`, `user_agent`/`fingerprint` (a file checksum as
# often as a browser one) and `host`/`hostname` (a machine, not a person) are absent per
# SPEC 4.4.1.
_ONLINE_IDENTIFIER_TOKENS = frozenset(
    {
        "ip",
        "ip_address",
        "ipaddr",
        "client_ip",
        "remote_ip",
        "remote_addr",
        "source_ip",
        "mac_address",
        "mac_addr",
        "device_id",
        "device_identifier",
        "advertising_id",
        "ad_id",
        "idfa",
        "gaid",
        "aaid",
        "session_id",
        "cookie_id",
        "visitor_id",
    },
)

# `looks_like` values that are themselves network or device identifiers. `uuid`,
# `numeric_string` and `base64` are absent - a UUID is the commonest primary key in any
# schema, and a session token satisfies the other two; session ids are carried by name only.
_ONLINE_IDENTIFIER_SHAPES = frozenset({"ip", "mac_address"})

# `looks_like` values that are themselves contact details.
_CONTACT_SHAPES = frozenset({"email", "phone"})

# `looks_like` values that are themselves financial accounts - each carries a mod-97 or a
# Luhn check, so it corroborates whatever the column name. `account_number`/`account_no`/
# `pan` are in no token set: each is as often an internal id, and the shape share alone
# carries them (SPEC 4.4.4).
_FINANCIAL_SHAPES = frozenset({"card_number", "iban"})

# Suffixes marking an organisation rather than a person; one is enough to veto the value.
_COMPANY_MARKERS = frozenset(
    {
        "inc",
        "llc",
        "ltd",
        "limited",
        "gmbh",
        "plc",
        "corp",
        "corporation",
        "co",
        "company",
        "sa",
        "ag",
        "bv",
        "nv",
        "srl",
        "spa",
        "pty",
        "llp",
        "partners",
        "holdings",
        "group",
        "foundation",
        "trust",
    },
)

_PERSON_TOKEN_RE = re.compile(r"^[A-Z][\w'-]*$", re.UNICODE)
_DIGIT_RE = re.compile(r"\d")

# US SSN only, excluding the area/group/serial ranges the SSA never issues: a UK NINO or a
# Canadian SIN will not corroborate, a stated single-locale gap (SPEC 4.4.4).
_SSN_RE = re.compile(r"^(\d{3})-(\d{2})-(\d{4})$")
_SSN_INVALID_AREAS = frozenset({"000", "666"})
_ADDRESS_RE = re.compile(EMAIL_ADDRESS)


def detect(
    column_name: str,
    values: Iterable[object],
    *,
    assigned: Mapping[LooksLike, int] | None = None,
    classification: str | None = None,
    table: str | None = None,
) -> Sensitivity | None:
    """Return the must-not-leave-the-database category this column carries, or None (SPEC 4.4).

    `assigned` is the per-pattern tally SPEC 4.1 assigned over `values`, whose length is the
    shape-share denominator; only string `values` corroborate a name. `table` settles a bare
    `name`, and a `boolean` carries none.
    """

    if classification == "boolean":
        return None

    normalized = _normalize(column_name)
    drawn = list(values)
    samples = [v for v in drawn if isinstance(v, str) and v.strip()]
    tally = assigned or {}

    if _is_personal_name(normalized, samples, table):
        return "personal_name"

    if _is_postal_address(normalized, samples):
        return "postal_address"

    if _is_geolocation(normalized, tally, len(drawn)):
        return "geolocation"

    if _is_date_of_birth(normalized):
        return "date_of_birth"

    if _is_national_id(normalized, samples):
        return "national_id"

    if _is_financial_account(normalized, tally, len(drawn)):
        return "financial_account"

    if _is_credential(normalized, tally, len(drawn), samples):
        return "credential"

    if _is_health(normalized):
        return "health"

    if _is_demographic(normalized):
        return "demographic"

    if _is_employment(normalized):
        return "employment"

    if _is_contact(normalized, tally, drawn):
        return "contact"

    if _is_online_identifier(normalized, tally, len(drawn)):
        return "online_identifier"

    return None


def names_credential(column_name: str) -> bool:
    """True when the column name alone makes a column a `credential` (SPEC 4.4.1)."""

    return _names_credential(_normalize(column_name))


def _normalize(column_name: str) -> str:
    """Lowercase and collapse separators so `firstName` and `first-name` agree."""

    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", column_name)

    return re.sub(r"[^a-z0-9]+", "_", spaced.lower()).strip("_")


def _matches(
    normalized: str,
    tokens: frozenset[str],
    disqualified_heads: frozenset[str] = frozenset(),
) -> bool:
    """True when a set member is the trailing token run of the normalized name.

    Token runs, never substrings: that is what keeps `username` and `filename` off the name
    axis (SPEC 4.4.3). Only the token immediately before the matched run is checked against
    `disqualified_heads`; any other head, including an empty one, neither qualifies nor vetoes.
    """

    name_parts = normalized.split("_")

    for token in tokens:
        token_parts = token.split("_")
        width = len(token_parts)

        if name_parts[-width:] != token_parts:
            continue

        head = name_parts[:-width]

        if head and head[-1] in disqualified_heads:
            continue

        return True

    return False


def _is_personal_name(normalized: str, samples: list[str], table: str | None) -> bool:
    if _thing_name_vetoed(normalized):
        return False

    if _matches(normalized, _STRONG_NAME_TOKENS):
        return True

    if not _matches(normalized, _WEAK_NAME_TOKENS):
        return False

    if normalized == "name" and table is not None and _names_a_non_person(table):
        return False

    return _share(samples, _looks_like_person) >= PERSONAL_NAME_VALUE_THRESHOLD


def _thing_name_vetoed(normalized: str) -> bool:
    """An organisation word vetoes the longest thing-name run; its trailing `name` is not retried."""

    name_parts = normalized.split("_")

    for token in sorted(_THING_NAME_TOKENS, key=lambda t: -t.count("_")):
        token_parts = token.split("_")

        if name_parts[-len(token_parts) :] == token_parts:
            head = name_parts[: -len(token_parts)]

            return bool(head) and head[-1] in _NAME_DISQUALIFIED_HEADS

    return False


def _is_postal_address(normalized: str, samples: list[str]) -> bool:
    if not _matches(normalized, _ADDRESS_TOKENS, _ADDRESS_DISQUALIFIED_HEADS):
        return False

    if not samples:
        return True

    return _share(samples, _looks_like_street) >= POSTAL_ADDRESS_VALUE_THRESHOLD


def _is_geolocation(normalized: str, tally: Mapping[LooksLike, int], drawn: int) -> bool:
    if _matches(normalized, _GEOLOCATION_TOKENS):
        return True

    return _shape_share(tally, _GEOLOCATION_SHAPES, drawn) >= GEOLOCATION_SHAPE_THRESHOLD


def _is_date_of_birth(normalized: str) -> bool:
    return _matches(normalized, _DATE_OF_BIRTH_TOKENS)


def _is_national_id(normalized: str, samples: list[str]) -> bool:
    if _matches(normalized, _STRONG_NATIONAL_ID_TOKENS):
        return True

    if _matches(normalized, _TAX_ID_TOKENS, _TAX_ID_DISQUALIFIED_HEADS):
        return True

    if not _matches(normalized, _WEAK_NATIONAL_ID_TOKENS):
        return False

    return _share(samples, _looks_like_ssn) >= NATIONAL_ID_VALUE_THRESHOLD


def _is_financial_account(normalized: str, tally: Mapping[LooksLike, int], drawn: int) -> bool:
    if _matches(normalized, _STRONG_FINANCIAL_ACCOUNT_TOKENS):
        return True

    return _shape_share(tally, _FINANCIAL_SHAPES, drawn) >= FINANCIAL_ACCOUNT_SHAPE_THRESHOLD


def _is_credential(
    normalized: str,
    tally: Mapping[LooksLike, int],
    drawn: int,
    samples: list[str],
) -> bool:
    if _names_credential(normalized):
        return True

    if _shape_share(tally, _CREDENTIAL_SHAPES, drawn) >= CREDENTIAL_SHAPE_THRESHOLD:
        return True

    if any(holds_secret(sample) for sample in samples):
        return True

    # Whole-string only, unlike every other tier: a tail match would flag `content_hash`
    # on a `hex` column as `credential`.
    return (
        normalized in _WEAK_CREDENTIAL_TOKENS
        and _shape_share(tally, _WEAK_CREDENTIAL_SHAPES, drawn) >= CREDENTIAL_SHAPE_THRESHOLD
    )


def _names_credential(normalized: str) -> bool:
    if _matches(normalized, _STRONG_CREDENTIAL_TOKENS):
        return True

    return normalized not in _COMPOUND_CREDENTIAL_TOKENS and _matches(
        normalized,
        _COMPOUND_CREDENTIAL_TOKENS,
    )


def _is_health(normalized: str) -> bool:
    return _matches(normalized, _HEALTH_TOKENS)


def _is_demographic(normalized: str) -> bool:
    return _matches(normalized, _DEMOGRAPHIC_TOKENS)


def _is_employment(normalized: str) -> bool:
    return _matches(normalized, _EMPLOYMENT_TOKENS, _EMPLOYMENT_DISQUALIFIED_HEADS)


def _is_contact(normalized: str, tally: Mapping[LooksLike, int], drawn: list[object]) -> bool:
    if _matches(normalized, _CONTACT_TOKENS):
        return True

    # A value assigned `email` always holds an address, so the search counts it and the tally
    # is read for the other shapes only - a list or a `Name <address>` matches no pattern whole.
    holding = sum(1 for v in drawn if isinstance(v, str) and _ADDRESS_RE.search(v))
    shapes = _CONTACT_SHAPES - {"email"}

    return _shape_share(tally, shapes, len(drawn), holding) >= CONTACT_SHAPE_THRESHOLD


def _is_online_identifier(normalized: str, tally: Mapping[LooksLike, int], drawn: int) -> bool:
    if _matches(normalized, _ONLINE_IDENTIFIER_TOKENS):
        return True

    return (
        _shape_share(tally, _ONLINE_IDENTIFIER_SHAPES, drawn) >= ONLINE_IDENTIFIER_SHAPE_THRESHOLD
    )


def _shape_share(
    tally: Mapping[LooksLike, int],
    shapes: frozenset[str],
    drawn: int,
    extra: int = 0,
) -> float:
    if not drawn:
        return 0.0

    return (sum(n for shape, n in tally.items() if shape in shapes) + extra) / drawn


def _names_a_non_person(table: str) -> bool:
    last = _normalize(table).split("_")[-1]
    singulars = {last, last.removesuffix("s"), last.removesuffix("es")}

    if last.endswith("ies"):
        singulars.add(last.removesuffix("ies") + "y")

    return not singulars.isdisjoint(_NAME_DISQUALIFIED_HEADS)


def _share(samples: list[str], predicate: Callable[[str], bool]) -> float:
    """Fraction of the sample satisfying `predicate`; an empty sample scores 0."""

    if not samples:
        return 0.0

    return sum(1 for s in samples if predicate(s)) / len(samples)


def _looks_like_person(value: str) -> bool:
    """Two or three capitalized word-tokens, no digits, no organisation marker."""

    if _DIGIT_RE.search(value):
        return False

    tokens = value.split()

    if not 2 <= len(tokens) <= 3:
        return False

    if any(t.strip(".,").lower() in _COMPANY_MARKERS for t in tokens):
        return False

    return all(_PERSON_TOKEN_RE.match(t) for t in tokens)


def _looks_like_street(value: str) -> bool:
    """A street line carries a number and at least two tokens."""

    return bool(_DIGIT_RE.search(value)) and len(value.split()) >= 2


def _looks_like_ssn(value: str) -> bool:
    """`NNN-NN-NNNN`, excluding the area/group/serial ranges the SSA never issues."""

    match = _SSN_RE.match(value)

    if not match:
        return False

    area, group, serial = match.groups()

    return (
        area not in _SSN_INVALID_AREAS
        and not area.startswith("9")
        and group != "00"
        and serial != "0000"
    )
