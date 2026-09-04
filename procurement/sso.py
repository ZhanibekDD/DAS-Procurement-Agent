"""DAS SSO-only adapter. No credential store, token cache, or local role authority."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import UUID


class SSOError(ValueError):
    def __init__(self, status: int = 403):
        super().__init__("DAS SSO access denied" if status != 503 else "DAS SSO temporarily unavailable")
        self.status = status


def origin(url: str) -> str:
    parts = urlsplit(url)
    port = parts.port
    default_port = 443 if parts.scheme == "https" else 80
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    return f"{parts.scheme}://{host}" + (f":{port}" if port and port != default_port else "")


def validate_config(settings) -> None:
    public = [urlsplit(settings.sso_authorize_url), urlsplit(settings.sso_redirect_uri)]
    internal = urlsplit(settings.sso_internal_base_url)
    for parts in [*public, internal]:
        if (parts.scheme not in {"http", "https"} or not parts.hostname
                or parts.username or parts.password or parts.query or parts.fragment):
            raise RuntimeError("SSO URLs must be absolute and contain no credentials, query, or fragment")
    for parts in public:
        if parts.scheme != "https" and parts.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise RuntimeError("SSO browser URLs require HTTPS except loopback canary")
    if (public[0].scheme, public[0].hostname) != (public[1].scheme, public[1].hostname):
        raise RuntimeError("SSO form_post requires same scheme and hostname for SameSite=Lax")
    if public[0].path != "/access/sso/authorize/" or public[1].path != "/auth/sso/callback":
        raise RuntimeError("SSO authorize/callback paths do not match the approved contract")
    if internal.path not in {"", "/"}:
        raise RuntimeError("SSO internal base must be an origin")
    if settings.sso_client_id != "procurement" or len(settings.sso_client_secret) < 32:
        raise RuntimeError("SSO requires the procurement client and a service secret of at least 32 characters")


def cookie_names(settings) -> tuple[str, str]:
    namespace = hashlib.sha256(settings.sso_redirect_uri.encode()).hexdigest()[:16]
    return f"__Host-procurement_sso_{namespace}", f"__Secure-procurement_state_{namespace}"


def _encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def seal(settings, purpose: str, payload: dict) -> str:
    encoded = _encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    binding = f"{purpose}:{settings.sso_client_id}:{settings.sso_redirect_uri}:{encoded}"
    signature = hmac.new(settings.auth_secret.encode(), binding.encode(), hashlib.sha256).digest()
    return encoded + "." + _encode(signature)


def unseal(settings, purpose: str, value: str) -> dict:
    if not value or len(value) > 16000:
        raise SSOError(401)
    try:
        encoded, signature = value.split(".", 1)
        if not re.fullmatch(r"[A-Za-z0-9_-]+", encoded) or not re.fullmatch(r"[A-Za-z0-9_-]+", signature):
            raise ValueError
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        payload = json.loads(raw)
        expected = seal(settings, purpose, payload).split(".", 1)[1]
        if not hmac.compare_digest(signature, expected):
            raise ValueError
        if (not isinstance(payload, dict) or type(payload.get("exp")) is not int
                or payload["exp"] <= int(time.time()) or payload["exp"] > int(time.time()) + 300):
            raise ValueError
        return payload
    except (ValueError, TypeError, KeyError, UnicodeError, AttributeError):
        raise SSOError(401) from None


def begin(settings) -> tuple[str, str]:
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(48)
    challenge = _encode(hashlib.sha256(verifier.encode()).digest())
    cookie = seal(settings, "state", {"state": state, "verifier": verifier, "exp": int(time.time()) + 300})
    query = urlencode({"client_id": settings.sso_client_id, "redirect_uri": settings.sso_redirect_uri,
        "state": state, "code_challenge": challenge, "code_challenge_method": "S256"})
    return settings.sso_authorize_url + "?" + query, cookie


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _post(settings, endpoint: str, payload: dict) -> dict:
    request = Request(settings.sso_internal_base_url.rstrip("/") + endpoint,
        data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json", "X-DAS-Client-Secret": settings.sso_client_secret})
    try:
        # No environment proxies or redirected credentials; normal certificate validation remains enabled.
        with build_opener(ProxyHandler({}), _NoRedirects()).open(request, timeout=3) as response:
            if response.status != 200:
                raise SSOError(503)
            raw = response.read(32769)
            if len(raw) > 32768:
                raise SSOError(503)
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise SSOError(503)
            return result
    except HTTPError as exc:
        raise SSOError(403 if exc.code in {401, 403} else 503) from None
    except (URLError, OSError, ValueError):
        raise SSOError(503) from None


def _access_token(result: dict) -> tuple[str, int]:
    token, expires = result.get("access_token"), result.get("expires_in")
    if (not isinstance(token, str) or not 16 <= len(token) <= 8192 or any(char.isspace() for char in token)
            or type(expires) is not int or not 1 <= expires <= 120):
        raise SSOError(503)
    return token, expires


def introspect(settings, token: str) -> dict:
    result = _post(settings, "/access/sso/introspect/", {"client_id": settings.sso_client_id, "token": token})
    if result.get("active") is not True:
        raise SSOError(403)
    try:
        subject = str(UUID(result["sub"]))
        if subject != result["sub"]:
            raise ValueError
        modules = result["modules"]
        if (not isinstance(modules, list) or not all(isinstance(item, str) for item in modules)
                or type(result["read_only"]) is not bool or type(result["epoch"]) is not int or result["epoch"] < 0
                or not isinstance(result["username"], str) or not 1 <= len(result["username"]) <= 128
                or not isinstance(result["email"], str) or len(result["email"]) > 320):
            raise ValueError
    except (ValueError, KeyError, TypeError, AttributeError):
        raise SSOError(503) from None
    if "procurement" not in modules:
        raise SSOError(403)
    token, expires = _access_token(result)
    return {"sub": subject, "username": result["username"], "email": result["email"], "modules": modules,
        "read_only": result["read_only"], "epoch": result["epoch"], "token": token, "expires": expires}


def complete(settings, state_cookie: str, code: str, state: str) -> dict:
    saved = unseal(settings, "state", state_cookie)
    if (not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", state)
            or not isinstance(saved.get("state"), str) or not hmac.compare_digest(saved["state"], state)
            or not re.fullmatch(r"[A-Za-z0-9_-]{43,128}", str(saved.get("verifier", "")))
            or not re.fullmatch(r"[A-Za-z0-9_-]{16,8192}", code)):
        raise SSOError(403)
    result = _post(settings, "/access/sso/token/", {"client_id": settings.sso_client_id, "code": code,
        "redirect_uri": settings.sso_redirect_uri, "code_verifier": saved["verifier"]})
    token, _ = _access_token(result)
    principal = introspect(settings, token)
    principal["csrf"] = secrets.token_urlsafe(32)
    return principal


def session_cookie(settings, principal: dict) -> str:
    return seal(settings, "session", {"token": principal["token"], "sub": principal["sub"],
        "csrf": principal["csrf"], "epoch": principal["epoch"], "exp": int(time.time()) + principal["expires"]})


def authenticate(settings, cookie: str) -> dict:
    saved = unseal(settings, "session", cookie)
    if (not isinstance(saved.get("token"), str) or not 16 <= len(saved["token"]) <= 8192
            or not re.fullmatch(r"[A-Za-z0-9_-]{40,128}", str(saved.get("csrf", "")))):
        raise SSOError(401)
    principal = introspect(settings, saved["token"])
    if principal["sub"] != saved.get("sub") or principal["epoch"] != saved.get("epoch"):
        raise SSOError(403)
    principal["csrf"] = saved["csrf"]
    return principal
