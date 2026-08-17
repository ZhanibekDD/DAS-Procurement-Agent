from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    environment: str
    api_key: str
    db_path: str
    outbox_mode: str
    sso_secret: str
    sso_issuer: str
    session_ttl_seconds: int

    @classmethod
    def from_env(cls) -> "Settings":
        settings = cls(
            environment=os.getenv("PROCUREMENT_ENV", "development").strip().lower(),
            api_key=os.getenv("PROCUREMENT_API_KEY", "").strip(),
            db_path=os.getenv("PROCUREMENT_DB_PATH", "./procurement.db").strip(),
            outbox_mode=os.getenv("PROCUREMENT_OUTBOX_MODE", "draft_only").strip(),
            sso_secret=os.getenv("PROCUREMENT_SSO_SECRET", "").strip(),
            sso_issuer=os.getenv("PROCUREMENT_SSO_ISSUER", "das").strip(),
            session_ttl_seconds=int(
                os.getenv("PROCUREMENT_SESSION_TTL_SECONDS", "28800").strip()
            ),
        )
        if settings.environment == "production" and not settings.api_key:
            raise RuntimeError("PROCUREMENT_API_KEY is required in production")
        if settings.outbox_mode != "draft_only":
            raise RuntimeError("MVP supports only PROCUREMENT_OUTBOX_MODE=draft_only")
        if settings.sso_secret and len(settings.sso_secret) < 32:
            raise RuntimeError("PROCUREMENT_SSO_SECRET must contain at least 32 characters")
        if not settings.sso_issuer:
            raise RuntimeError("PROCUREMENT_SSO_ISSUER must not be empty")
        if not 300 <= settings.session_ttl_seconds <= 86_400:
            raise RuntimeError(
                "PROCUREMENT_SESSION_TTL_SECONDS must be between 300 and 86400"
            )
        return settings

