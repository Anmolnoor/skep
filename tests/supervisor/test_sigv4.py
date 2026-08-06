"""v108-F6: the hand-rolled SigV4 signer, pinned to AWS's published vectors.

Bedrock signs instead of bearing a token, and skep signs without botocore, so
these vectors ARE the proof of correctness: the derived-signing-key example
from the AWS docs and two cases from AWS's own sigv4 test suite (``get-vanilla``
and ``get-vanilla-query-order-key-case``). Every signature here is
deterministic — the signing time is passed in, never read from the clock.
"""

from __future__ import annotations

from datetime import UTC, datetime

from skep.supervisor.serve.sigv4 import (
    ALGORITHM,
    AwsCredentials,
    canonical_request,
    credentials_from_env,
    sign_request,
    signing_key,
    string_to_sign,
)

# The AWS test-suite identity (AKIDEXAMPLE + the documented example secret).
_ACCESS_KEY = "AKIDEXAMPLE"
_SECRET_KEY = "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"
_CREDENTIALS = AwsCredentials(_ACCESS_KEY, _SECRET_KEY)
_SUITE_NOW = datetime(2015, 8, 30, 12, 36, 0, tzinfo=UTC)
_EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def test_signing_key_matches_the_documented_derivation() -> None:
    """AWS docs, "Deriving the signing key": 20120215 / us-east-1 / iam."""
    key = signing_key(_SECRET_KEY, datestamp="20120215", region="us-east-1", service="iam")
    assert key.hex() == "f4780e2d9f65fa895f9c67b32ce1baf0b0d8a43505a000a1a9e090d414db404d"


def test_canonical_request_and_string_to_sign_for_get_vanilla() -> None:
    headers = {"Host": "example.amazonaws.com", "X-Amz-Date": "20150830T123600Z"}
    request, signed_headers = canonical_request(
        method="GET",
        url="http://example.amazonaws.com/",
        headers=headers,
        payload_hash=_EMPTY_SHA256,
    )
    assert signed_headers == "host;x-amz-date"
    assert request == (
        "GET\n"
        "/\n"
        "\n"
        "host:example.amazonaws.com\n"
        "x-amz-date:20150830T123600Z\n"
        "\n"
        "host;x-amz-date\n"
        f"{_EMPTY_SHA256}"
    )
    scope = "20150830/us-east-1/service/aws4_request"
    assert string_to_sign(request=request, amz_date="20150830T123600Z", scope=scope).splitlines()[
        :3
    ] == [
        ALGORITHM,
        "20150830T123600Z",
        "20150830/us-east-1/service/aws4_request",
    ]


def test_get_vanilla_authorization_header() -> None:
    headers = sign_request(
        method="GET",
        url="http://example.amazonaws.com/",
        headers={},
        payload=b"",
        region="us-east-1",
        service="service",
        credentials=_CREDENTIALS,
        now=_SUITE_NOW,
    )
    assert headers["x-amz-date"] == "20150830T123600Z"
    assert headers["host"] == "example.amazonaws.com"
    assert headers["Authorization"] == (
        f"{ALGORITHM} Credential=AKIDEXAMPLE/20150830/us-east-1/service/aws4_request, "
        "SignedHeaders=host;x-amz-date, "
        "Signature=5fa00fa31553b73ebf1942676e86291e8372ff2a2260956d9b8aae1d763fbf31"
    )


def test_query_parameters_are_canonicalized_in_key_order() -> None:
    """``get-vanilla-query-order-key-case``: the same signature either way round."""
    expected = "b97d918cfa904a5beff61c982a1b6f458b799221646efd99d3219ec94cdf2500"
    for query in ("Param1=value1&Param2=value2", "Param2=value2&Param1=value1"):
        headers = sign_request(
            method="GET",
            url=f"http://example.amazonaws.com/?{query}",
            headers={},
            payload=b"",
            region="us-east-1",
            service="service",
            credentials=_CREDENTIALS,
            now=_SUITE_NOW,
        )
        assert headers["Authorization"].endswith(f"Signature={expected}")


def test_session_token_is_signed_and_sent() -> None:
    headers = sign_request(
        method="GET",
        url="http://example.amazonaws.com/",
        headers={},
        payload=b"",
        region="us-east-1",
        service="service",
        credentials=AwsCredentials(_ACCESS_KEY, _SECRET_KEY, "sts-token-123"),
        now=_SUITE_NOW,
    )
    assert headers["x-amz-security-token"] == "sts-token-123"
    assert "SignedHeaders=host;x-amz-date;x-amz-security-token" in headers["Authorization"]
    # A temporary credential signs to something else than the same keys without it.
    assert not headers["Authorization"].endswith(
        "Signature=5fa00fa31553b73ebf1942676e86291e8372ff2a2260956d9b8aae1d763fbf31"
    )


def test_body_and_headers_change_the_signature_deterministically() -> None:
    def sign(payload: bytes) -> str:
        return sign_request(
            method="POST",
            url="https://bedrock-runtime.us-east-1.amazonaws.com/model/m/converse-stream",
            headers={"Content-Type": "application/json"},
            payload=payload,
            region="us-east-1",
            service="bedrock",
            credentials=_CREDENTIALS,
            now=_SUITE_NOW,
        )["Authorization"]

    assert sign(b'{"a":1}') == sign(b'{"a":1}')  # same inputs, same signature
    assert sign(b'{"a":1}') != sign(b'{"a":2}')  # the payload hash is signed
    assert "SignedHeaders=content-type;host;x-amz-date" in sign(b"{}")


def test_path_segments_are_encoded_twice_like_every_non_s3_service() -> None:
    """A bedrock model id carries a ``:``; the wire path escapes it once and
    the canonical request escapes it again (botocore does exactly this)."""
    request, _ = canonical_request(
        method="POST",
        url="https://bedrock-runtime.us-east-1.amazonaws.com/model/anthropic.claude%3A0/invoke",
        headers={"host": "bedrock-runtime.us-east-1.amazonaws.com"},
        payload_hash=_EMPTY_SHA256,
    )
    assert request.splitlines()[1] == "/model/anthropic.claude%253A0/invoke"


def test_credentials_come_from_the_environment_and_need_both_halves() -> None:
    assert credentials_from_env({"AWS_ACCESS_KEY_ID": "AKIA", "AWS_SECRET_ACCESS_KEY": ""}) is None
    assert credentials_from_env({"AWS_SECRET_ACCESS_KEY": "s"}) is None
    credentials = credentials_from_env(
        {
            "AWS_ACCESS_KEY_ID": "AKIA",
            "AWS_SECRET_ACCESS_KEY": "secret",
            "AWS_SESSION_TOKEN": "tok",
        }
    )
    assert credentials == AwsCredentials("AKIA", "secret", "tok")
