"""AWS Signature Version 4, signed by hand (v108-F6).

Bedrock is the one provider whose auth is a *signature* rather than a bearer
token, and the official signer ships inside botocore — a 20MB dependency tree
for ~80 lines of HMAC. This module is those lines: canonical request →
string-to-sign → derived signing key → ``Authorization`` header, with nothing
but the standard library.

The signing time is an explicit parameter, never read here — every signature
is reproducible, and the unit tests pin fixed-date vectors from the AWS spec.

Scope, honestly stated: no chunked/streaming payload signing (the whole
request body is hashed up front), no URI path normalization of ``.``/``..``
segments, and no presigned-URL query auth. Bedrock's Converse endpoints need
none of it. Path segments are quoted once here, which double-encodes an
already-percent-encoded path — that is the documented rule for every service
except S3, and it is what botocore does.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import parse_qsl, quote, urlsplit

ALGORITHM = "AWS4-HMAC-SHA256"
_REQUEST_TERMINATOR = "aws4_request"
_UNSIGNED_HEADERS = frozenset({"authorization", "content-length", "user-agent", "expect"})


@dataclass(frozen=True)
class AwsCredentials:
    """A SigV4 key PAIR (plus the STS token when the keys are temporary)."""

    access_key: str
    secret_key: str
    session_token: str | None = None


def credentials_from_env(env: Mapping[str, str] | None = None) -> AwsCredentials | None:
    """The daemon environment's AWS keys, or None when either half is missing.

    Deliberately env-only: skep's one secret file holds a single opaque
    string, and a signature needs two halves plus an optional token.
    """
    source = os.environ if env is None else env
    access_key = (source.get("AWS_ACCESS_KEY_ID") or "").strip()
    secret_key = (source.get("AWS_SECRET_ACCESS_KEY") or "").strip()
    if not access_key or not secret_key:
        return None
    return AwsCredentials(
        access_key=access_key,
        secret_key=secret_key,
        session_token=(source.get("AWS_SESSION_TOKEN") or "").strip() or None,
    )


def _hmac(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def _sha256_hex(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def signing_key(secret_key: str, *, datestamp: str, region: str, service: str) -> bytes:
    """The four-step derived key: date → region → service → terminator."""
    key_date = _hmac(f"AWS4{secret_key}".encode(), datestamp)
    key_region = _hmac(key_date, region)
    key_service = _hmac(key_region, service)
    return _hmac(key_service, _REQUEST_TERMINATOR)


def _canonical_query(query: str) -> str:
    pairs = sorted(parse_qsl(query, keep_blank_values=True))
    return "&".join(
        f"{quote(name, safe='-_.~')}={quote(value, safe='-_.~')}" for name, value in pairs
    )


def canonical_request(
    *, method: str, url: str, headers: Mapping[str, str], payload_hash: str
) -> tuple[str, str]:
    """(canonical request, signed-header list) for ``headers`` exactly as sent."""
    split = urlsplit(url)
    path = quote(split.path or "/", safe="/~")
    signed = sorted(
        (name.lower(), " ".join(value.strip().split()))
        for name, value in headers.items()
        if name.lower() not in _UNSIGNED_HEADERS
    )
    signed_headers = ";".join(name for name, _ in signed)
    canonical_headers = "".join(f"{name}:{value}\n" for name, value in signed)
    request = "\n".join(
        [
            method.upper(),
            path,
            _canonical_query(split.query),
            canonical_headers,
            signed_headers,
            payload_hash,
        ]
    )
    return request, signed_headers


def string_to_sign(*, request: str, amz_date: str, scope: str) -> str:
    return "\n".join([ALGORITHM, amz_date, scope, _sha256_hex(request.encode("utf-8"))])


def sign_request(
    *,
    method: str,
    url: str,
    headers: Mapping[str, str],
    payload: bytes,
    region: str,
    service: str,
    credentials: AwsCredentials,
    now: datetime,
) -> dict[str, str]:
    """``headers`` plus the SigV4 set — send exactly these with exactly ``payload``.

    ``host`` is returned too, so the header that was signed is the header that
    goes on the wire (a client filling in its own Host would break the
    signature the moment a port appeared).
    """
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    datestamp = now.strftime("%Y%m%d")
    scope = f"{datestamp}/{region}/{service}/{_REQUEST_TERMINATOR}"
    signed_headers_map = {name: value for name, value in headers.items() if value is not None}
    signed_headers_map["host"] = urlsplit(url).netloc
    signed_headers_map["x-amz-date"] = amz_date
    if credentials.session_token:
        signed_headers_map["x-amz-security-token"] = credentials.session_token
    request, signed_headers = canonical_request(
        method=method,
        url=url,
        headers=signed_headers_map,
        payload_hash=_sha256_hex(payload),
    )
    signature = hmac.new(
        signing_key(credentials.secret_key, datestamp=datestamp, region=region, service=service),
        string_to_sign(request=request, amz_date=amz_date, scope=scope).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    signed_headers_map["Authorization"] = (
        f"{ALGORITHM} Credential={credentials.access_key}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    return signed_headers_map
