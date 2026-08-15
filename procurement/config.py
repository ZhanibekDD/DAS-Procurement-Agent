from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    environment: str
    api_key: str
    db_path: str
    outbox_mode: str

    @classmethod
    def from_env(cls) -> "Settings":
        settings = cls(
            environment=os.getenv("PROCUREMENT_ENV", "development").strip().lower(),
            api_key=os.getenv("PROCUREMENT_API_KEY", "").strip(),
            db_path=os.getenv("PROCUREMENT_DB_PATH", "./procurement.db").strip(),
            outbox_mode=os.getenv("PROCUREMENT_OUTBOX_MODE", "draft_only").strip(),
        )
        if settings.environment == "production" and not settings.api_key:
            raise RuntimeError("PROCUREMENT_API_KEY is required in production")
        if settings.outbox_mode != "draft_only":
            raise RuntimeError("MVP supports only PROCUREMENT_OUTBOX_MODE=draft_only")
        return settings

