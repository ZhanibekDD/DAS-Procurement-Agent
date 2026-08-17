from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import hmac
import secrets


_SCHEME = "scrypt"
_N = 2**14
_R = 8
_P = 1
_DKLEN = 32
_SALT_BYTES = 16
_MAX_PASSWORD_CHARS = 256


class PasswordHashError(ValueError):
    """Raised when a password or stored password hash is invalid."""


def _encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes:
    try:
        value.encode("ascii")
        padding = "=" * (-len(value) % 4)
        return base64.b64decode(value + padding, altchars=b"-_", validate=True)
    except (UnicodeEncodeError, ValueError, TypeError) as exc:
        raise PasswordHashError("invalid password hash encoding") from exc


def _derive(password: str, salt: bytes) -> bytes:
    if not isinstance(password, str) or not 1 <= len(password) <= _MAX_PASSWORD_CHARS:
        raise PasswordHashError("password length is invalid")
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_N,
        r=_R,
        p=_P,
        dklen=_DKLEN,
        maxmem=64 * 1024 * 1024,
    )


def _parse(encoded_hash: str) -> tuple[bytes, bytes]:
    try:
        scheme, n_value, r_value, p_value, salt_value, digest_value = encoded_hash.split(":")
        if scheme != _SCHEME or (int(n_value), int(r_value), int(p_value)) != (_N, _R, _P):
            raise PasswordHashError("unsupported password hash parameters")
        salt = _decode(salt_value)
        digest = _decode(digest_value)
    except (AttributeError, ValueError) as exc:
        if isinstance(exc, PasswordHashError):
            raise
        raise PasswordHashError("invalid password hash format") from exc
    if len(salt) != _SALT_BYTES or len(digest) != _DKLEN:
        raise PasswordHashError("invalid password hash length")
    return salt, digest


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    """Create a server-side scrypt password hash. Plaintext is never stored."""
    selected_salt = secrets.token_bytes(_SALT_BYTES) if salt is None else salt
    if len(selected_salt) != _SALT_BYTES:
        raise PasswordHashError("salt must contain exactly 16 bytes")
    digest = _derive(password, selected_salt)
    return f"{_SCHEME}:{_N}:{_R}:{_P}:{_encode(selected_salt)}:{_encode(digest)}"


def verify_password(password: str, encoded_hash: str) -> bool:
    """Fail closed for malformed hashes, oversized passwords, and mismatches."""
    try:
        salt, expected = _parse(encoded_hash)
        actual = _derive(password, salt)
    except (PasswordHashError, UnicodeError, ValueError):
        return False
    return hmac.compare_digest(actual, expected)


def validate_password_hash(encoded_hash: str) -> bool:
    try:
        _parse(encoded_hash)
    except PasswordHashError:
        return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate PROCUREMENT_ADMIN_PASSWORD_HASH without exposing plaintext in shell history."
    )
    parser.parse_args()
    password = getpass.getpass("New Procurement admin password: ")
    confirmation = getpass.getpass("Repeat password: ")
    if password != confirmation:
        parser.error("passwords do not match")
    if len(password) < 12:
        parser.error("password must contain at least 12 characters")
    print(hash_password(password))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
