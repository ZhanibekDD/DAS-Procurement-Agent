from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from ipaddress import ip_network
from pathlib import Path

from .passwords import validate_password_hash


_USERNAME_RE = re.compile(r"^[A-Za-z0-9_.@-]{3,128}$")


def _read_secret(name: str) -> str:
    direct = os.getenv(name, "")
    filename = os.getenv(f"{name}_FILE", "").strip()
    if direct and filename:
        raise RuntimeError(f"{name} and {name}_FILE cannot be configured together")
    if not filename:
        return direct.strip()
    path = Path(filename)
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError(f"cannot read {name}_FILE") from exc


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name, "true" if default else "false").strip().casefold()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"{name} must be true or false")


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
    mail_address: str = ""
    mail_username: str = ""
    mail_password: str = field(default="", repr=False)
    mail_imap_host: str = "imap.timeweb.ru"
    mail_imap_port: int = 993
    mail_smtp_host: str = "smtp.timeweb.ru"
    mail_smtp_port: int = 465
    mail_receive_enabled: bool = False
    mail_send_enabled: bool = False
    mail_sync_interval_seconds: int = 120
    mail_sync_batch_size: int = 100
    ingest_binary: str = "/usr/local/bin/das-ingest"
    ingest_timeout_seconds: int = 20

    @property
    def mail_credentials_configured(self) -> bool:
        return bool(self.mail_address and self.mail_username and self.mail_password)

    @property
    def local_auth_configured(self) -> bool:
        return bool(
            self.auth_secret and self.admin_username and self.admin_password_hash
        )

    @classmethod
    def from_env(cls) -> "Settings":
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
            mail_address=os.getenv("PROCUREMENT_MAIL_ADDRESS", "").strip().lower(),
            mail_username=os.getenv(
                "PROCUREMENT_MAIL_USERNAME",
                os.getenv("PROCUREMENT_MAIL_ADDRESS", ""),
            ).strip().lower(),
            mail_password=_read_secret("PROCUREMENT_MAIL_PASSWORD"),
            mail_imap_host=os.getenv(
                "PROCUREMENT_MAIL_IMAP_HOST", "imap.timeweb.ru"
            ).strip(),
            mail_imap_port=int(
                os.getenv("PROCUREMENT_MAIL_IMAP_PORT", "993").strip()
            ),
            mail_smtp_host=os.getenv(
                "PROCUREMENT_MAIL_SMTP_HOST", "smtp.timeweb.ru"
            ).strip(),
            mail_smtp_port=int(
                os.getenv("PROCUREMENT_MAIL_SMTP_PORT", "465").strip()
            ),
            mail_receive_enabled=_env_bool("PROCUREMENT_MAIL_RECEIVE_ENABLED"),
            mail_send_enabled=_env_bool("PROCUREMENT_MAIL_SEND_ENABLED"),
            mail_sync_interval_seconds=int(
                os.getenv("PROCUREMENT_MAIL_SYNC_INTERVAL_SECONDS", "120").strip()
            ),
            mail_sync_batch_size=int(
                os.getenv("PROCUREMENT_MAIL_SYNC_BATCH_SIZE", "100").strip()
            ),
            ingest_binary=os.getenv(
                "PROCUREMENT_INGEST_BINARY", "/usr/local/bin/das-ingest"
            ).strip(),
            ingest_timeout_seconds=int(
                os.getenv("PROCUREMENT_INGEST_TIMEOUT_SECONDS", "20").strip()
            ),
        )
        if settings.environment == "production" and not settings.api_key:
            raise RuntimeError("PROCUREMENT_API_KEY is required in production")
        if settings.outbox_mode != "draft_only":
            raise RuntimeError("MVP supports only PROCUREMENT_OUTBOX_MODE=draft_only")

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
        if not 300 <= settings.session_ttl_seconds <= 86_400:
            raise RuntimeError(
                "PROCUREMENT_SESSION_TTL_SECONDS must be between 300 and 86400"
            )
        if settings.mail_address and (
            "@" not in settings.mail_address
            or "\n" in settings.mail_address
            or "\r" in settings.mail_address
        ):
            raise RuntimeError("PROCUREMENT_MAIL_ADDRESS is invalid")
        if (settings.mail_receive_enabled or settings.mail_send_enabled) and not (
            settings.mail_credentials_configured
        ):
            raise RuntimeError(
                "mail address username and password are required when mail is enabled"
            )
        if not 1 <= settings.mail_imap_port <= 65535:
            raise RuntimeError("PROCUREMENT_MAIL_IMAP_PORT is invalid")
        if not 1 <= settings.mail_smtp_port <= 65535:
            raise RuntimeError("PROCUREMENT_MAIL_SMTP_PORT is invalid")
        if not 30 <= settings.mail_sync_interval_seconds <= 86_400:
            raise RuntimeError(
                "PROCUREMENT_MAIL_SYNC_INTERVAL_SECONDS must be between 30 and 86400"
            )
        if not 1 <= settings.mail_sync_batch_size <= 500:
            raise RuntimeError(
                "PROCUREMENT_MAIL_SYNC_BATCH_SIZE must be between 1 and 500"
            )
        if not settings.ingest_binary:
            raise RuntimeError("PROCUREMENT_INGEST_BINARY must not be empty")
        if not 1 <= settings.ingest_timeout_seconds <= 120:
            raise RuntimeError(
                "PROCUREMENT_INGEST_TIMEOUT_SECONDS must be between 1 and 120"
            )
        return settings
