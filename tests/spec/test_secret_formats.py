"""Each recognised secret format, found alone and inside prose; nothing merely like one."""

from __future__ import annotations

import hashlib
import itertools
import uuid

import pytest
from hypothesis import given
from hypothesis import strategies as st

from dbprint.spec.secret_formats import holds_secret, secret_spans
from tests._secrets import ALNUM, CRYPT, FORMATS, UPPER, b64url, jwt, pem, pick


class TestEveryFormat:
    @pytest.mark.parametrize("name", sorted(FORMATS))
    def test_a_value_that_is_the_secret(self, name: str) -> None:
        secret = FORMATS[name]()

        assert secret_spans(secret) == ((0, len(secret)),)
        assert holds_secret(secret)

    @pytest.mark.parametrize("name", sorted(FORMATS))
    def test_a_secret_inside_prose(self, name: str) -> None:
        secret = FORMATS[name]()
        text = f"rotated after the audit: {secret} (old one revoked)"
        start = text.index(secret)

        assert secret_spans(text) == ((start, start + len(secret)),)


class TestNearMisses:
    @pytest.mark.parametrize(
        "value",
        [
            pytest.param("$2" + "b$12$" + pick(CRYPT, 52), id="bcrypt one short"),
            pytest.param("$2" + "b$12$" + pick(CRYPT, 54), id="bcrypt one long"),
            pytest.param("gh" + "p_" + pick(ALNUM, 35), id="classic token one short"),
            pytest.param("gh" + "p_" + pick(ALNUM, 37), id="classic token one long"),
            pytest.param("AK" + "IA" + pick(UPPER, 17), id="access key id one long"),
            pytest.param("x" + "AK" + "IA" + pick(UPPER, 16), id="access key id after a letter"),
            pytest.param("sk" + "_live_", id="secret key prefix with no body"),
            pytest.param(hashlib.sha256(b"seedbank").hexdigest(), id="sha256 hex digest"),
            pytest.param(str(uuid.UUID(int=7)), id="uuid"),
            pytest.param("see the vault record for the field notes", id="prose"),
        ],
    )
    def test_nothing_is_found(self, value: str) -> None:
        assert secret_spans(value) == ()
        assert not holds_secret(value)


class TestPrivateKeys:
    def test_the_span_runs_from_begin_through_end(self) -> None:
        key = pem()
        text = f"keyfile: {key}\nnext: line"

        assert secret_spans(text) == ((9, 9 + len(key)),)

    def test_a_header_with_no_footer_spans_its_body(self) -> None:
        key = pem().split("\n-----END")[0]

        assert secret_spans(key) == ((0, len(key)),)


class TestJwt:
    def test_one_inside_an_authorization_header(self) -> None:
        token = jwt()
        text = f"Authorization: Bearer {token}"

        assert secret_spans(text) == ((22, 22 + len(token)),)

    @pytest.mark.parametrize(
        "value",
        [
            pytest.param(jwt({"kid": "seedbank"}), id="header without alg"),
            pytest.param(
                "eyJ" + b64url({"alg": "HS256"})[3:] + "x." + b64url({"a": 1}) + ".sig",
                id="header not json",
            ),
            pytest.param(jwt() + ".extra", id="a fourth segment"),
            pytest.param("x" + jwt(), id="after a letter"),
        ],
    )
    def test_a_dotted_token_that_is_no_jwt(self, value: str) -> None:
        assert secret_spans(value) == ()


@given(st.text(alphabet=ALNUM + "$_-./=+ :\n", max_size=200), st.sampled_from(sorted(FORMATS)))
def test_spans_lie_inside_the_text_and_never_overlap(noise: str, name: str) -> None:
    text = noise + " " + FORMATS[name]() + " " + noise
    spans = secret_spans(text)

    assert spans
    assert all(0 <= start < end <= len(text) for start, end in spans)
    assert all(left[1] <= right[0] for left, right in itertools.pairwise(spans))
    assert holds_secret(text) == bool(spans)
