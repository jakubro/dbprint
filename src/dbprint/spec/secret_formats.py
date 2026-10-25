"""The secret formats SPEC 4.4.3 recognises in a value, wherever in the value one occurs.

Password hashes, keys with a fixed issuer prefix and body, PEM private keys and JWTs: none occurs
in ordinary data by accident, so one occurrence makes a value a secret. Pure: no I/O, no state.
"""

from __future__ import annotations

import re

from .looks_like import is_jwt


def secret_spans(text: str) -> tuple[tuple[int, int], ...]:
    """Every `(start, end)` a recognised format occupies in `text`, in order, overlaps merged."""

    spans = [match.span() for match in _FORMATS_RE.finditer(text)]
    spans += [m.span() for m in _JWT_CANDIDATE_RE.finditer(text) if is_jwt(m.group(0))]
    merged: list[tuple[int, int]] = []

    for start, end in sorted(spans):
        if merged and start < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))

    return tuple(merged)


def holds_secret(value: str) -> bool:
    """True when `value` holds at least one recognised secret format."""

    return _FORMATS_RE.search(value) is not None or any(
        is_jwt(m.group(0)) for m in _JWT_CANDIDATE_RE.finditer(value)
    )


_CRYPT = r"[./A-Za-z0-9]"
_B64 = r"[A-Za-z0-9+/]+={0,2}"
_FORMATS = (
    rf"\$2[aby]\$\d{{2}}\${_CRYPT}{{53}}(?!{_CRYPT})",
    rf"\$argon2(?:id|i|d)\$v=\d+\$m=\d+,t=\d+,p=\d+\${_B64}\${_B64}",
    rf"\$scrypt\$ln=\d+,r=\d+,p=\d+\${_B64}\${_B64}",
    rf"\$[56]\$(?:rounds=\d+\$)?{_CRYPT}{{1,16}}\${_CRYPT}{{43,86}}",
    rf"pbkdf2_sha(?:1|256|512)\$\d+\$[^$\s]+\${_B64}",
    r"(?:sk_live_|sk_test_|rk_live_)[0-9A-Za-z]{24,}",
    r"whsec_[A-Za-z0-9+/=]{24,}",
    r"gh[po]_[A-Za-z0-9]{36}(?![A-Za-z0-9])",
    r"github_pat_[A-Za-z0-9]{22}_[A-Za-z0-9]{59}(?![A-Za-z0-9])",
    r"glpat-[A-Za-z0-9_-]{20,}",
    r"xox[bp]-\d+-[0-9A-Za-z-]{10,}",
    r"AKIA[0-9A-Z]{16}(?![0-9A-Z])",
    r"AIza[0-9A-Za-z_-]{35}(?![0-9A-Za-z_-])",
    (
        r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----[A-Za-z0-9+/=\s\\]*"
        r"(?:-----END (?:[A-Z]+ )*PRIVATE KEY-----)?"
    ),
)
_FORMATS_RE = re.compile("|".join(f"(?<![A-Za-z0-9])(?:{branch})" for branch in _FORMATS))
_JWT_CANDIDATE_RE = re.compile(
    r"(?<![A-Za-z0-9_.-])eyJ[A-Za-z0-9_-]*\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*"
    r"(?![A-Za-z0-9_-]|\.[A-Za-z0-9_-])",
)
