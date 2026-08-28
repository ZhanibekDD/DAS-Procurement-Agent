from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from typing import Any


_ID_RE = re.compile(r"^[A-Za-z0-9_.:@-]{1,128}$")
_JTI_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_TOKEN_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_ALLOWED_ROLES = {"admin", "staff"}
_MAX_TOKEN_TTL_SECONDS = 90 * 24 * 60 * 60


class TokenError(ValueError):
    """Raised when a signed Procurement session token is not trustworthy."""


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    try:
        padding = "=" * (-len(value) % 4)
        return base64.b64decode(value + padding, altchars=b"-_", validate=True)
    except (ValueError, TypeError) as exc:
        raise TokenError("invalid token encoding") from exc


def issue_token(
    secret: str,
    *,
    issuer: str,
    audience: str,
    subject: str,
    role: str,
    kind: str,
    ttl_seconds: int,
    now: int | None = None,
    jti: str | None = None,
) -> str:
    if len(secret) < 32:
        raise TokenError("token secret must contain at least 32 characters")
    if not _ID_RE.fullmatch(issuer) or not _ID_RE.fullmatch(audience):
        raise TokenError("invalid issuer or audience")
    if not _ID_RE.fullmatch(subject):
        raise TokenError("invalid subject")
    if role not in _ALLOWED_ROLES:
        raise TokenError("invalid role")
    if not _ID_RE.fullmatch(kind):
        raise TokenError("invalid token kind")
    if not 1 <= ttl_seconds <= _MAX_TOKEN_TTL_SECONDS:
        raise TokenError("invalid token ttl")

    issued_at = int(time.time()) if now is None else int(now)
    token_id = jti or secrets.token_urlsafe(24)
    if not _JTI_RE.fullmatch(token_id):
        raise TokenError("invalid token id")

    claims = {
        "aud": audience,
        "exp": issued_at + ttl_seconds,
        "iat": issued_at,
        "iss": issuer,
        "jti": token_id,
        "kind": kind,
        "role": role,
        "sub": subject,
    }
    payload = json.dumps(
        claims,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    encoded = _b64encode(payload)
    signature = hmac.new(secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).digest()
    return f"{encoded}.{_b64encode(signature)}"


def verify_token(
    secret: str,
    token: str,
    *,
    issuer: str,
    audience: str,
    kind: str,
    max_ttl_seconds: int,
    now: int | None = None,
) -> dict[str, Any]:
    if len(secret) < 32:
        raise TokenError("token verification is not configured")
    try:
        encoded, supplied_signature = token.split(".", 1)
    except ValueError as exc:
        raise TokenError("invalid token format") from exc
    if (
        not _TOKEN_SEGMENT_RE.fullmatch(encoded)
        or not _TOKEN_SEGMENT_RE.fullmatch(supplied_signature)
    ):
        raise TokenError("invalid token encoding")

    expected_signature = hmac.new(
        secret.encode("utf-8"),
        encoded.encode("ascii"),
        hashlib.sha256,
    ).digest()
    if not hmac.compare_digest(_b64encode(expected_signature), supplied_signature):
        raise TokenError("invalid token signature")

    try:
        claims = json.loads(_b64decode(encoded))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise TokenError("invalid token payload") from exc
    if not isinstance(claims, dict):
        raise TokenError("invalid token claims")

    required = {"aud", "exp", "iat", "iss", "jti", "kind", "role", "sub"}
    if not required.issubset(claims):
        raise TokenError("missing token claims")
    if claims["iss"] != issuer or claims["aud"] != audience or claims["kind"] != kind:
        raise TokenError("token scope mismatch")
    if not _ID_RE.fullmatch(str(claims["sub"])):
        raise TokenError("invalid subject")
    if claims["role"] not in _ALLOWED_ROLES:
        raise TokenError("invalid role")
    if not _JTI_RE.fullmatch(str(claims["jti"])):
        raise TokenError("invalid token id")
    if type(claims["iat"]) is not int or type(claims["exp"]) is not int:
        raise TokenError("invalid token timestamps")

    current = int(time.time()) if now is None else int(now)
    if claims["iat"] > current + 30:
        raise TokenError("token issued in the future")
    if claims["exp"] <= current:
        raise TokenError("token expired")
    ttl = claims["exp"] - claims["iat"]
    if ttl < 1 or ttl > max_ttl_seconds:
        raise TokenError("token ttl exceeds policy")
    return claims
