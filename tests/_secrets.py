"""Synthetic secrets in every format `spec/secret_formats.py` recognises, built at run time.

Each prefix is assembled from pieces and each body drawn from a seeded generator, so no literal in
the tree reads as a live key to a secret scanner.
"""

from __future__ import annotations

import base64
import json
import random
import string
from collections.abc import Callable


ALNUM = string.ascii_letters + string.digits
B64 = ALNUM + "+/"
CRYPT = ALNUM + "./"
URLSAFE = ALNUM + "_-"
UPPER = string.ascii_uppercase + string.digits
_RNG = random.Random(1018)


def pick(alphabet: str, n: int) -> str:
    """`n` characters drawn from `alphabet`."""

    return "".join(_RNG.choice(alphabet) for _ in range(n))


def b64url(document: dict[str, object]) -> str:
    """A JSON object as one unpadded base64url segment."""

    return base64.urlsafe_b64encode(json.dumps(document).encode()).rstrip(b"=").decode()


def jwt(header: dict[str, object] | None = None) -> str:
    """A three-segment token; the default header names `alg`, as a JOSE header does."""

    head = b64url(header or {"alg": "HS256", "typ": "JWT"})

    return f"{head}.{b64url({'sub': pick(string.digits, 6)})}.{pick(URLSAFE, 43)}"


def pem() -> str:
    """A private key in PEM armour, header through footer."""

    body = "\n".join(pick(B64, 64) for _ in range(4))

    return "-----BEGIN " + "PRIVATE KEY-----\n" + body + "\n-----END " + "PRIVATE KEY-----"


FORMATS: dict[str, Callable[[], str]] = {
    "bcrypt": lambda: "$2" + "b$12$" + pick(CRYPT, 53),
    "argon2": lambda: "$argon2" + "id$v=19$m=65536,t=3,p=4$" + pick(B64, 22) + "$" + pick(B64, 43),
    "scrypt": lambda: "$scr" + "ypt$ln=16,r=8,p=1$" + pick(B64, 22) + "$" + pick(B64, 43),
    "sha512crypt": lambda: "$6" + "$rounds=5000$" + pick(CRYPT, 16) + "$" + pick(CRYPT, 86),
    "sha256crypt": lambda: "$5" + "$" + pick(CRYPT, 16) + "$" + pick(CRYPT, 43),
    "pbkdf2": lambda: "pbk" + "df2_sha256$600000$" + pick(ALNUM, 22) + "$" + pick(B64, 43) + "=",
    "sk_live": lambda: "sk" + "_live_" + pick(ALNUM, 24),
    "sk_test": lambda: "sk" + "_test_" + pick(ALNUM, 24),
    "rk_live": lambda: "rk" + "_live_" + pick(ALNUM, 24),
    "whsec": lambda: "wh" + "sec_" + pick(ALNUM, 32),
    "ghp": lambda: "gh" + "p_" + pick(ALNUM, 36),
    "gho": lambda: "gh" + "o_" + pick(ALNUM, 36),
    "github_pat": lambda: "github" + "_pat_" + pick(ALNUM, 22) + "_" + pick(ALNUM, 59),
    "glpat": lambda: "gl" + "pat-" + pick(ALNUM, 20),
    "xoxb": lambda: "xo" + "xb-" + pick(string.digits, 11) + "-" + pick(ALNUM, 24),
    "xoxp": lambda: "xo" + "xp-" + pick(string.digits, 12) + "-" + pick(string.hexdigits, 32),
    "akia": lambda: "AK" + "IA" + pick(UPPER, 16),
    "aiza": lambda: "AI" + "za" + pick(URLSAFE, 35),
    "pem": pem,
    "jwt": jwt,
}
