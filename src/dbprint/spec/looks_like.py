"""looks_like pattern detection per SPEC v1, section 4.1.

`detect()` returns the pattern matching >= 95% of a sample, or None; `detect_with_evidence()`
also returns the draw size and match tally the verdict rests on. Pure: no I/O.
"""

from __future__ import annotations

import base64
import ipaddress
import json
import re
import zoneinfo
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date
from typing import Literal


LooksLike = Literal[
    "uuid",
    "email",
    "url",
    "urn",
    "content_type",
    "path",
    "ip",
    "mac_address",
    "country_code",
    "currency_code",
    "postal_code",
    "isbn",
    "ean",
    "imei",
    "card_number",
    "iban",
    "bic",
    "phone",
    "timezone",
    "json",
    "hex",
    "jwt",
    "vin",
    "base64",
    "latlon",
    "iso8601_duration",
    "iso8601_date",
    "iso8601_datetime",
    "numeric_string",
    "semver",
    "filename",
    "prose",
]

MATCH_THRESHOLD = 0.95


_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-7][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
_URL_RE = re.compile(r"^https?://[^\s]+$")
# RFC 8141: urn:<namespace-id>:<namespace-specific-string>, case-insensitive.
_URN_RE = re.compile(r"^urn:[A-Za-z0-9][A-Za-z0-9-]{0,30}:\S+$", re.IGNORECASE)
# Six hex octet pairs joined by one consistent separator, colon or hyphen.
_MAC_ADDRESS_RE = re.compile(
    r"^([0-9a-fA-F]{2})([:-])"
    r"(?:[0-9a-fA-F]{2}\2){4}[0-9a-fA-F]{2}$",
)
_BASE64_STD = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")
_BASE64_URLSAFE = re.compile(r"^[A-Za-z0-9_\-]+={0,2}$")
# base64url alphabet, no padding - RFC 7515's compact serialization never pads a segment.
_JWT_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_-]*$")
_NUMERIC_STRING_RE = re.compile(r"^-?\d+(\.\d+)?$")
# RFC 6838: a registered top-level type, a restricted-name subtype, optional parameters.
# The type half is the closed registry, or any two-segment path (`a/image.png`) matches.
_CONTENT_TYPE_RE = re.compile(
    r"^(?:application|audio|example|font|image|message|model|multipart|text|video"
    r"|x-[A-Za-z0-9!#$&^_.+-]+)"
    r"/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,126}"
    r"(?:\s*;\s*\S.*)?$",
    re.IGNORECASE,
)
# POSIX only, and a scheme disqualifies it so a URL never reads as a path.
_PATH_RE = re.compile(r"^(?!\w+://)/?(?:[^/\s]+/)+[^/\s]*$")
# The extension must start with a letter, or an IPv4 address and a decimal read as filenames.
_FILENAME_RE = re.compile(r"^[^/\\\s]+\.[A-Za-z][A-Za-z0-9]{0,9}$")

# semver.org's reference grammar plus an optional leading `v`, which git tags store constantly.
_SEMVER_RE = re.compile(
    r"^v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*)?"
    r"(?:\+[0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*)?$",
)

# ISO 3166-1 alpha-2. A closed set, so membership is the whole test.
_COUNTRY_CODES_ALPHA2 = frozenset(
    """
    AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ BA BB BD BE BF BG BH BI BJ
    BL BM BN BO BQ BR BS BT BV BW BY BZ CA CC CD CF CG CH CI CK CL CM CN CO CR
    CU CV CW CX CY CZ DE DJ DK DM DO DZ EC EE EG EH ER ES ET FI FJ FK FM FO FR
    GA GB GD GE GF GG GH GI GL GM GN GP GQ GR GS GT GU GW GY HK HM HN HR HT HU
    ID IE IL IM IN IO IQ IR IS IT JE JM JO JP KE KG KH KI KM KN KP KR KW KY KZ
    LA LB LC LI LK LR LS LT LU LV LY MA MC MD ME MF MG MH MK ML MM MN MO MP MQ
    MR MS MT MU MV MW MX MY MZ NA NC NE NF NG NI NL NO NP NR NU NZ OM PA PE PF
    PG PH PK PL PM PN PR PS PT PW PY QA RE RO RS RU RW SA SB SC SD SE SG SH SI
    SJ SK SL SM SN SO SR SS ST SV SX SY SZ TC TD TF TG TH TJ TK TL TM TN TO TR
    TT TV TW TZ UA UG UM US UY UZ VA VC VE VG VI VN VU WF WS YE YT ZA ZM ZW
    """.split(),  # noqa: SIM905 - 249 quoted pairs are far less reviewable than this block
)

# ISO 4217, snapshot dated 2026-01-01; three letters cannot collide with the alpha-2 set, so
# membership is the whole test. A missing code is a known staleness gap (SPEC 4.1.1), not a bug.
_CURRENCY_CODES = frozenset(
    """
    AED AFN ALL AMD AOA ARS AUD AWG AZN BAM BBD BDT BHD BIF BMD BND BOB BOV BRL
    BSD BTN BWP BYN BZD CAD CDF CHE CHF CHW CLF CLP CNY COP COU CRC CUP CVE CZK
    DJF DKK DOP DZD EGP ERN ETB EUR FJD FKP GBP GEL GHS GIP GMD GNF GTQ GYD HKD
    HNL HTG HUF IDR ILS INR IQD IRR ISK JMD JOD JPY KES KGS KHR KMF KPW KRW KWD
    KYD KZT LAK LBP LKR LRD LSL LYD MAD MDL MGA MKD MMK MNT MOP MRU MUR MVR MWK
    MXN MXV MYR MZN NAD NGN NIO NOK NPR NZD OMR PAB PEN PGK PHP PKR PLN PYG QAR
    RON RSD RUB RWF SAR SBD SCR SDG SEK SGD SHP SLE SOS SRD SSP STN SVC SYP SZL
    THB TJS TMT TND TOP TRY TTD TWD TZS UAH UGX USD USN UYI UYU UYW UZS VED VES
    VND VUV WST XAD XAF XAG XAU XBA XBB XBC XBD XCD XCG XDR XOF XPD XPF XPT XSU
    XTS XUA XXX YER ZAR ZMW ZWG
    """.split(),  # noqa: SIM905 - 155 quoted triples are far less reviewable than this block
)

# IANA zone names, bound once - `available_timezones()` walks TZPATH on every call.
# Membership is case-sensitive and the set is never filtered: keeping whatever the standard
# library reports avoids a second hand-maintained literal (SPEC 4.1.1).
_TIMEZONES = frozenset(zoneinfo.available_timezones())

# Postal codes whose format identifies itself: each carries a letter. An all-digit code is
# indistinguishable from `numeric_string` without a locale, which dbprint never has.
_POSTAL_CODE_RES = (
    re.compile(r"^[A-Z]{1,2}\d[A-Z\d]? ?\d[A-Z]{2}$", re.IGNORECASE),  # United Kingdom
    re.compile(r"^[A-Z]\d[A-Z] ?\d[A-Z]\d$", re.IGNORECASE),  # Canada
    re.compile(r"^\d{4} ?[A-Z]{2}$", re.IGNORECASE),  # Netherlands
)

# VIN: 17 characters; I/O/Q are excluded, having no value in Table III's transliteration.
_VIN_CHARS_RE = re.compile(r"^[0-9A-HJ-NPR-Z]{17}$")

# Federal VIN regulation (49 C.F.R. 565.15), Table III: check-digit transliteration.
_VIN_TRANSLITERATION = {
    "A": 1,
    "B": 2,
    "C": 3,
    "D": 4,
    "E": 5,
    "F": 6,
    "G": 7,
    "H": 8,
    "J": 1,
    "K": 2,
    "L": 3,
    "M": 4,
    "N": 5,
    "P": 7,
    "R": 9,
    "S": 2,
    "T": 3,
    "U": 4,
    "V": 5,
    "W": 6,
    "X": 7,
    "Y": 8,
    "Z": 9,
}

# Federal VIN regulation, Table IV: positional weights; position 9, the check digit, weighs 0.
_VIN_WEIGHTS = (8, 7, 6, 5, 4, 3, 2, 10, 0, 9, 8, 7, 6, 5, 4, 3, 2)

_IMEI_RE = re.compile(r"^\d{15}$")

# GSMA PRD TS.06 Annex A: every Reporting Body Identifier ever allocated, active and retired.
# A stale entry mislabels an IMEI as `card_number` rather than dropping it; neither `34` nor
# `37` (Amex's issuer prefixes) has ever appeared here.
_IMEI_ALLOCATED_RBI = frozenset(
    {
        "00",  # test IMEIs
        "01",  # CTIA, United States
        "35",  # TUV SUD / BABT, United Kingdom
        "86",  # TAF, China
        "98",  # reserved
        "99",  # Global Hexadecimal Administrator
        "10",  # DECT PP with GSM functionality
        "30",  # Iridium
        "33",  # DGPT / ART, France
        "44",  # BABT (original), United Kingdom
        "45",  # NTA, Denmark
        "49",  # BZT / BAPT / Reg TP, Germany
        "50",  # BZT ETS Certification, Germany
        "51",  # Cetecom ICT Services, Germany
        "52",  # CETECOM, Germany
        "53",  # TUV Product Service Munich, Germany
        "54",  # Phoenix Test-Lab, Germany
        "91",  # MSAI, India (suspended 2019)
    },
)

# Separator-tolerant PAN digit run; Luhn is the discriminator, not the shape.
_CARD_NUMBER_CHARS_RE = re.compile(r"^[\d -]+$")

# ISO 13616: country, check digits, alphanumerics; the 15-34 length bound is checked separately.
_IBAN_RE = re.compile(r"^[A-Z]{2}\d{2}[0-9A-Z]{0,30}$")

# SWIFT BIC8/BIC11: institution + country + location, optional branch code.
_BIC_RE = re.compile(r"^[A-Z]{6}[0-9A-Z]{2}([0-9A-Z]{3})?$")

# Optional `#` (color codes), then 6+ hex digits. The letter requirement is enforced
# separately: it keeps an all-digit id as `numeric_string` and `EC1A1BB` as `postal_code`.
_HEX_RE = re.compile(r"^#?[0-9a-fA-F]{6,}$")

# Two decimal degrees, comma-separated, each with a fractional part; range-checked separately.
_LATLON_RE = re.compile(r"^(-?\d+\.\d+), ?(-?\d+\.\d+)$")

# P[nY][nM][nD][T[nH][nM][nS]] or the PnW week form; `P` and `PT` alone are refused.
_ISO8601_DURATION_RE = re.compile(
    r"^P(?!$)(?:\d+Y)?(?:\d+M)?(?:\d+D)?(?:T(?=\d)(?:\d+H)?(?:\d+M)?(?:\d+S)?)?$|^P\d+W$",
)

_ISO8601_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")

# `T` or a single space (what Postgres's `timestamp::text` renders); seconds and the zone
# designator are optional. Case-sensitive: lowercase `t`/`z` are RFC 3339-legal but unemitted.
_ISO8601_DATETIME_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:\d{2})?$",
)

# Character set for both phone forms; no dot - a version string would qualify (SPEC 4.1.1).
_PHONE_CHARS_RE = re.compile(r"^\+?[0-9 ()\-]+$")
_PHONE_SEPARATOR_RE = re.compile(r"[ ()\-]")

# Prose needs both a token count and a function word: either alone admits a code list or a label.
_PROSE_MIN_TOKENS = 5
_PROSE_FUNCTION_WORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "been",
        "but",
        "by",
        "for",
        "from",
        "has",
        "have",
        "in",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "that",
        "the",
        "there",
        "this",
        "to",
        "was",
        "were",
        "which",
        "with",
        "you",
        "your",
    },
)
_PROSE_WORD_RE = re.compile(r"[a-z']+")


@dataclass(frozen=True)
class LooksLikeMatch:
    """A `detect()` verdict plus the evidence it was scored against.

    `sampled` and `matched` are both `0` only when the sample itself was empty, never when a
    pattern failed to clear the threshold - `pattern` alone carries that distinction.
    """

    pattern: LooksLike | None
    sampled: int
    matched: int


def detect(values: Iterable[object]) -> LooksLike | None:
    """Return the pattern assigned to >= 95% of the sample, or None.

    Each value is coerced with `str()` and assigned exactly one pattern - the first in
    priority order - before anything is counted; the denominator is the whole sample,
    including values that matched nothing (SPEC 4.1.1, 4.1.3).
    """

    return detect_with_evidence(values).pattern


def detect_with_evidence(values: Iterable[object]) -> LooksLikeMatch:
    """Like `detect`, but also returns the draw size and the winning pattern's tally.

    `sampled` and `matched` are what SPEC 4.1.3's 95% threshold is computed from, so a
    consumer recomputes `matched / sampled` to judge how much evidence a verdict rests on -
    a two-of-two match reads identically to a ten-thousand-of-ten-thousand one without them.
    """

    sample = list(values)

    if not sample:
        return LooksLikeMatch(None, 0, 0)

    stringified = (v if isinstance(v, str) else str(v) for v in sample)
    assigned: Counter[LooksLike] = Counter(p for p in map(_assign, stringified) if p is not None)

    for pattern, _ in _PRIORITY:
        if assigned[pattern] / len(sample) >= MATCH_THRESHOLD:
            return LooksLikeMatch(pattern, len(sample), assigned[pattern])

    return LooksLikeMatch(None, len(sample), 0)


def _assign(value: str) -> LooksLike | None:
    """The first pattern in priority order this value matches, if any."""

    for pattern, matcher in _PRIORITY:
        if matcher(value):
            return pattern

    return None


def _match_uuid(s: str) -> bool:
    return bool(_UUID_RE.match(s))


def _match_email(s: str) -> bool:
    return bool(_EMAIL_RE.match(s))


def _match_url(s: str) -> bool:
    return bool(_URL_RE.match(s))


def _match_urn(s: str) -> bool:
    return bool(_URN_RE.match(s))


def _match_ip(s: str) -> bool:
    """A bare IPv4 or IPv6 address; CIDR, a port, a chain, brackets and a zone are not.

    A zone index (`fe80::1%eth0`) needs explicit rejection; the parser rejects brackets and CIDR.
    """

    if "%" in s:
        return False

    try:
        ipaddress.ip_address(s)
    except ValueError:
        return False

    return True


def _match_mac_address(s: str) -> bool:
    """Six hex octet pairs, colon- or hyphen-joined; separatorless is `hex` instead."""

    return bool(_MAC_ADDRESS_RE.match(s))


def _match_json(s: str) -> bool:
    try:
        parsed = json.loads(s)
    except (ValueError, TypeError):
        return False

    return isinstance(parsed, (dict, list))


def _match_hex(s: str) -> bool:
    if not _HEX_RE.match(s):
        return False

    return any(c in "abcdefABCDEF" for c in s)


def _match_jwt(s: str) -> bool:
    """RFC 7515 compact serialization whose header decodes to a JOSE object.

    Three dot-segments; the signature may be empty (the `alg: none` unsecured form) and is
    never decoded. The header decode is what separates a JWT from any other dotted token.
    """

    parts = s.split(".")

    if len(parts) != 3:
        return False

    header, payload, _signature = parts

    if not header or not payload:
        return False

    if not (_JWT_SEGMENT_RE.match(header) and _JWT_SEGMENT_RE.match(payload)):
        return False

    try:
        decoded = base64.urlsafe_b64decode(header + "=" * (-len(header) % 4))
        parsed = json.loads(decoded)
    except (ValueError, TypeError):
        return False

    return isinstance(parsed, dict) and "alg" in parsed


def _match_base64(s: str) -> bool:
    """Length, alphabet, real padding, a case mixture, and a clean decode.

    An unpadded URL-safe candidate is accepted only where its length is already a multiple of
    four; padding a short remainder would admit any label not congruent to 1 mod 4. Requiring
    both cases separates an encoded token from a lowercase word or an all-uppercase code.
    """

    if len(s) < 16:
        return False

    if not (_BASE64_STD.match(s) or _BASE64_URLSAFE.match(s)):
        return False

    if not (any(c.isupper() for c in s) and any(c.islower() for c in s)):
        return False

    try:
        if _BASE64_URLSAFE.match(s) and not _BASE64_STD.match(s):
            if len(s) % 4 != 0:
                return False

            base64.urlsafe_b64decode(s)
        else:
            base64.b64decode(s, validate=True)
    except (ValueError, TypeError):
        return False

    return True


def _match_latlon(s: str) -> bool:
    match = _LATLON_RE.match(s)

    if not match:
        return False

    lat, lon = float(match.group(1)), float(match.group(2))

    return -90 <= lat <= 90 and -180 <= lon <= 180


def _match_iso8601_duration(s: str) -> bool:
    return bool(_ISO8601_DURATION_RE.match(s))


def _match_iso8601_date(s: str) -> bool:
    match = _ISO8601_DATE_RE.match(s)

    return bool(match) and _is_real_calendar_date(match)


def _match_iso8601_datetime(s: str) -> bool:
    match = _ISO8601_DATETIME_RE.match(s)

    return bool(match) and _is_real_calendar_date(match)


def _is_real_calendar_date(match: re.Match[str]) -> bool:
    """Gate a date-shaped regex match against the calendar - `2024-02-31` is not a date."""

    try:
        date(*(int(g) for g in match.groups()[:3]))
    except ValueError:
        return False

    return True


def _match_numeric_string(s: str) -> bool:
    return bool(_NUMERIC_STRING_RE.match(s))


def _match_content_type(s: str) -> bool:
    return bool(_CONTENT_TYPE_RE.match(s))


def _match_path(s: str) -> bool:
    """A POSIX path, excluding a value that also parses as a network block.

    CIDR notation (`10.0.0.0/8`) satisfies `_PATH_RE`'s segment-slash-segment shape, so a
    network block reports nothing rather than the wrong pattern.
    """

    if not _PATH_RE.match(s):
        return False

    try:
        ipaddress.ip_network(s, strict=False)
    except ValueError:
        return True

    return False


def _match_filename(s: str) -> bool:
    return bool(_FILENAME_RE.match(s))


def _match_semver(s: str) -> bool:
    return bool(_SEMVER_RE.match(s))


def _match_country_code(s: str) -> bool:
    return s.upper() in _COUNTRY_CODES_ALPHA2


def _match_currency_code(s: str) -> bool:
    return s.upper() in _CURRENCY_CODES


def _match_postal_code(s: str) -> bool:
    return any(pattern.match(s) for pattern in _POSTAL_CODE_RES)


def _match_isbn(s: str) -> bool:
    """The 13-digit ISBN (GS1 mod-10, `978`/`979` prefix) or the 10-digit form (mod-11, `X` check).

    Hyphens and spaces are stripped first; hyphenated forms are stored constantly.
    """

    stripped = s.replace("-", "").replace(" ", "").upper()

    if len(stripped) == 13:
        if not (stripped.isdigit() and stripped.startswith(("978", "979"))):
            return False

        return _gs1_mod10_valid(stripped)

    if len(stripped) == 10:
        return _isbn10_check_valid(stripped)

    return False


def _isbn10_check_valid(s: str) -> bool:
    total = 0

    for i, ch in enumerate(s):
        if ch == "X" and i == 9:
            value = 10
        elif ch.isdigit():
            value = int(ch)
        else:
            return False

        total += value * (10 - i)

    return total % 11 == 0


def _match_ean(s: str) -> bool:
    """GTIN of 8, 12, 13 or 14 digits, GS1 mod-10 valid; no prefix table, unlike `isbn`."""

    return len(s) in (8, 12, 13, 14) and s.isdigit() and _gs1_mod10_valid(s)


def _gs1_mod10_valid(digits: str) -> bool:
    """GS1 mod-10: weights 1 and 3 alternating from the right; shared by `isbn` and `ean`."""

    total = 0

    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        total += d if i % 2 == 0 else d * 3

    return total % 10 == 0


def _match_vin(s: str) -> bool:
    """17 chars, alphabet A-Z/0-9 minus I/O/Q, check digit at position 9 (49 C.F.R. 565.15)."""

    upper = s.upper()

    if not _VIN_CHARS_RE.match(upper):
        return False

    total = sum(
        (int(ch) if ch.isdigit() else _VIN_TRANSLITERATION[ch]) * weight
        for ch, weight in zip(upper, _VIN_WEIGHTS, strict=True)
    )
    remainder = total % 11
    expected = "X" if remainder == 10 else str(remainder)

    return upper[8] == expected


def _match_imei(s: str) -> bool:
    """Fifteen digits, Luhn-valid, first two digits an allocated Reporting Body Identifier.

    The RBI gate is the only thing separating an IMEI from a Luhn-valid PAN (SPEC 4.1.1).
    """

    return bool(_IMEI_RE.match(s)) and _luhn_valid(s) and s[:2] in _IMEI_ALLOCATED_RBI


def _match_card_number(s: str) -> bool:
    """A PAN: 13-19 digits once spaces and hyphens are stripped, Luhn-valid.

    Separator-tolerant like `phone` - the Luhn check is precise enough on its own (SPEC 4.1.1).
    """

    if not _CARD_NUMBER_CHARS_RE.match(s):
        return False

    digits = s.replace(" ", "").replace("-", "")

    if not 13 <= len(digits) <= 19:
        return False

    return _luhn_valid(digits)


def _luhn_valid(digits: str) -> bool:
    total = 0

    for i, ch in enumerate(reversed(digits)):
        d = int(ch)

        if i % 2 == 1:
            d *= 2

            if d > 9:
                d -= 9

        total += d

    return total % 10 == 0


def _match_iban(s: str) -> bool:
    """ISO 7064 mod-97-10, over the rearranged, digit-substituted form.

    Spaces stripped, case folded; the 15-34 length bound is checked outside the regex.
    """

    stripped = s.replace(" ", "").upper()

    if not 15 <= len(stripped) <= 34 or not _IBAN_RE.match(stripped):
        return False

    rearranged = stripped[4:] + stripped[:4]
    numeric = "".join(str(int(ch, 36)) for ch in rearranged)

    return int(numeric) % 97 == 1


def _match_bic(s: str) -> bool:
    """8 or 11 chars; positions 5-6 must be a real ISO 3166 alpha-2 country."""

    if not _BIC_RE.match(s):
        return False

    return s[4:6] in _COUNTRY_CODES_ALPHA2


def _match_phone(s: str) -> bool:
    """E.164, or a separator-bearing national form; a bare digit run is not a phone.

    The digit floors exclude a US SSN (9 digits) and an ISO date (8) arithmetically
    (SPEC 4.1.1); only the unprefixed form needs a separator, which keeps a bare account
    number or order id out.
    """

    if not _PHONE_CHARS_RE.match(s):
        return False

    has_plus = s.startswith("+")
    digits = _PHONE_SEPARATOR_RE.sub("", s[1:] if has_plus else s)

    if has_plus:
        return 8 <= len(digits) <= 15

    return bool(_PHONE_SEPARATOR_RE.search(s)) and 10 <= len(digits) <= 15


def _match_timezone(s: str) -> bool:
    """Exact, case-sensitive membership - `europe/london` is not a zone."""

    return s in _TIMEZONES


def _match_prose(s: str) -> bool:
    """Free-running text: enough tokens, at least one function word, no structure.

    The structural exclusion is `prose`'s own definition (SPEC 4.1.1), not just priority order.
    """

    if len(s.split()) < _PROSE_MIN_TOKENS:
        return False

    if any(matcher(s) for _, matcher in _STRUCTURAL):
        return False

    return any(word in _PROSE_FUNCTION_WORDS for word in _PROSE_WORD_RE.findall(s.lower()))


# Structural patterns, in SPEC 4.1.4 priority order. `prose` is the fallthrough and sits
# outside this tuple because its own predicate consults it.
#
# `path` and `filename` rank last: a base64 token, compact JSON, a dotted decimal and a
# `semver` prerelease (`1.0.0-alpha.beta`) all satisfy `filename`'s shape. `semver` need not
# outrank `numeric_string` - a two-part version stays numeric, and a three-part one already
# fails its single-dot grammar. `urn` outranks `path` too - `urn:example:weather/today`
# carries the `/` `path` requires, and `urn:uuid:...` would otherwise fall through
# everything, since `uuid` excludes the URN form.
#
# `isbn` outranks `ean`: the 13-digit ISBN is the `978`/`979` subset of the 13-digit EAN
# under the identical GS1 check, so the subset must be tested first or its rank never fires.
# Both outrank `card_number`, whose Luhn test they pass about one time in ten; the loser of
# a collision reports nothing rather than the wrong shape. `imei` outranks `card_number` on
# the same arithmetic - with no IIN table, every fifteen-digit IMEI is Luhn-valid at card
# length, and the allocated-RBI gate is the discriminator. `card_number` and `mac_address`
# both outrank `phone`: a spaced Amex number and `00-11-22-33-44-55` are digit runs with
# separators, so Luhn and the octet shape are what tell them apart (SPEC 4.1.1).
#
# `hex` outranks `base64`: every digest length in circulation (32, 40, 64) is a multiple of
# four over a subset of the base64 alphabet. `vin` outranks it too, clearing its length floor
# and alphabet on 17 characters; `jwt` outranks it defensively, nothing above it claiming a
# dotted token. `postal_code` outranks `hex`, since a compact UK postcode (`EC1A1BB`) is
# coincidentally all-hex. `timezone` outranks `path` and `base64`
# (`Europe/London` is segment-slash-segment, and a long zone name draws from the base64
# alphabet), and `country_code` outranks `timezone`, since IANA `backward` aliases (`GB`,
# `NZ`) are also alpha-2 codes.
_STRUCTURAL: tuple[tuple[LooksLike, Callable[[str], bool]], ...] = (
    ("uuid", _match_uuid),
    ("email", _match_email),
    ("url", _match_url),
    ("urn", _match_urn),
    ("content_type", _match_content_type),
    ("ip", _match_ip),
    ("mac_address", _match_mac_address),
    ("country_code", _match_country_code),
    ("currency_code", _match_currency_code),
    ("postal_code", _match_postal_code),
    ("isbn", _match_isbn),
    ("ean", _match_ean),
    ("imei", _match_imei),
    ("card_number", _match_card_number),
    ("iban", _match_iban),
    ("bic", _match_bic),
    ("phone", _match_phone),
    ("timezone", _match_timezone),
    ("json", _match_json),
    ("hex", _match_hex),
    ("jwt", _match_jwt),
    ("vin", _match_vin),
    ("base64", _match_base64),
    ("latlon", _match_latlon),
    ("iso8601_duration", _match_iso8601_duration),
    ("iso8601_date", _match_iso8601_date),
    ("iso8601_datetime", _match_iso8601_datetime),
    ("numeric_string", _match_numeric_string),
    ("semver", _match_semver),
    ("path", _match_path),
    ("filename", _match_filename),
)

_PRIORITY: tuple[tuple[LooksLike, Callable[[str], bool]], ...] = (
    *_STRUCTURAL,
    ("prose", _match_prose),
)
