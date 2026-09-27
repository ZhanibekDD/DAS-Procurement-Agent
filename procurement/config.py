from __future__ import annotations

import os
import re
from dataclasses import dataclass
from ipaddress import ip_network

from .passwords import validate_password_hash


_USERNAME_RE = re.compile(r"^[A-Za-z0-9_.@-]{3,128}$")


@dataclass(frozen=True)
class Settings:
    environment: str
    api_key: str
    db_path: str
    outbox_mode: str
    auth_secret: str
    admin_username: str
    admin_password_hash: str
    session_ttl_seconds: int
    trusted_proxy_networks: tuple[str, ...] = ()
    sso_enabled: bool = False
    sso_authorize_url: str = ""
    sso_internal_base_url: str = ""
    sso_client_id: str = "procurement"
    sso_client_secret: str = ""
    sso_redirect_uri: str = ""

    @property
    def local_auth_configured(self) -> bool:
        return bool(
            not self.sso_enabled and self.auth_secret and self.admin_username and self.admin_password_hash
        )

    @classmethod
    def from_env(cls) -> "Settings":
        sso_keys = ("DAS_SSO_AUTHORIZE_URL", "DAS_SSO_INTERNAL_BASE_URL", "DAS_SSO_CLIENT_ID",
                    "DAS_SSO_CLIENT_SECRET", "DAS_SSO_REDIRECT_URI")
        sso_enabled = any(key in os.environ for key in sso_keys)
        trusted_proxy_values = tuple(
            value.strip()
            for value in os.getenv("PROCUREMENT_TRUSTED_PROXY_IPS", "").split(",")
            if value.strip()
        )
        try:
            trusted_proxy_networks = tuple(
                str(ip_network(value, strict=False)) for value in trusted_proxy_values
            )
        except ValueError as exc:
            raise RuntimeError(
                "PROCUREMENT_TRUSTED_PROXY_IPS contains an invalid IP or network"
            ) from exc

        settings = cls(
            environment=os.getenv("PROCUREMENT_ENV", "development").strip().lower(),
            api_key=os.getenv("PROCUREMENT_API_KEY", "").strip(),
            db_path=os.getenv("PROCUREMENT_DB_PATH", "./procurement.db").strip(),
            outbox_mode=os.getenv("PROCUREMENT_OUTBOX_MODE", "draft_only").strip(),
            auth_secret=os.getenv("PROCUREMENT_AUTH_SECRET", "").strip(),
            admin_username=os.getenv("PROCUREMENT_ADMIN_USERNAME", "").strip(),
            admin_password_hash=os.getenv(
                "PROCUREMENT_ADMIN_PASSWORD_HASH", ""
            ).strip(),
            session_ttl_seconds=int(
                os.getenv("PROCUREMENT_SESSION_TTL_SECONDS", "28800").strip()
            ),
            trusted_proxy_networks=trusted_proxy_networks,
            sso_enabled=sso_enabled,
            sso_authorize_url=os.getenv("DAS_SSO_AUTHORIZE_URL", "").strip(),
            sso_internal_base_url=os.getenv("DAS_SSO_INTERNAL_BASE_URL", "").strip(),
            sso_client_id=os.getenv("DAS_SSO_CLIENT_ID", "procurement").strip(),
            sso_client_secret=os.getenv("DAS_SSO_CLIENT_SECRET", "").strip(),
            sso_redirect_uri=os.getenv("DAS_SSO_REDIRECT_URI", "").strip(),
        )
        if settings.environment == "production" and not settings.api_key and not settings.sso_enabled:
            raise RuntimeError("PROCUREMENT_API_KEY is required in production")
        if settings.outbox_mode != "draft_only":
            raise RuntimeError("MVP supports only PROCUREMENT_OUTBOX_MODE=draft_only")
        if not 300 <= settings.session_ttl_seconds <= 86_400:
            raise RuntimeError("PROCUREMENT_SESSION_TTL_SECONDS must be between 300 and 86400")

        if settings.sso_enabled:
            from .sso import validate_config
            if settings.admin_username or settings.admin_password_hash:
                raise RuntimeError("SSO mode forbids local admin credentials")
            if len(settings.auth_secret) < 32:
                raise RuntimeError("SSO state signing requires PROCUREMENT_AUTH_SECRET of at least 32 characters")
            validate_config(settings)
            return settings

        auth_values = (
            settings.auth_secret,
            settings.admin_username,
            settings.admin_password_hash,
        )
        if any(auth_values) and not all(auth_values):
            raise RuntimeError(
                "PROCUREMENT_AUTH_SECRET, PROCUREMENT_ADMIN_USERNAME and "
                "PROCUREMENT_ADMIN_PASSWORD_HASH must be configured together"
            )
        if settings.auth_secret and len(settings.auth_secret) < 32:
            raise RuntimeError(
                "PROCUREMENT_AUTH_SECRET must contain at least 32 characters"
            )
        if settings.admin_username and not _USERNAME_RE.fullmatch(
            settings.admin_username
        ):
            raise RuntimeError("PROCUREMENT_ADMIN_USERNAME has an invalid format")
        if settings.admin_password_hash and not validate_password_hash(
            settings.admin_password_hash
        ):
            raise RuntimeError("PROCUREMENT_ADMIN_PASSWORD_HASH is invalid")
        if (
            settings.environment == "production"
            and not settings.local_auth_configured
        ):
            raise RuntimeError(
                "standalone Procurement authentication is required in production"
            )
        return settings
