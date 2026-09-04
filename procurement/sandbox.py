"""Local-only adapter contract. No sockets, transports, URLs or credentials are used."""
from __future__ import annotations

import hashlib
import json


def payload_sha256(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


def message_fingerprint(message: dict) -> str:
    fields = ("id", "campaign_id", "supplier_id", "channel", "recipient", "subject", "body",
              "lot_id", "lot_cluster", "project_cluster", "supplier_cluster")
    return payload_sha256({key: message[key] for key in fields})


class SandboxAdapter:
    channel = ""

    def simulate(self, message: dict, fingerprint: str) -> dict:
        if message["status"] != "approved" or not message["approved_by"] or not message["approved_at"]:
            raise ValueError("human approval is required before sandbox simulation")
        if message["channel"] != self.channel or message_fingerprint(message) != fingerprint:
            raise ValueError("sandbox payload or channel does not match approval")
        return {"mode": "sandbox", "status": "simulated", "channel": self.channel,
                "message_id": message["id"], "receipt_id": f"sandbox-{self.channel}-{fingerprint}",
                "payload_sha256": fingerprint, "external_send": False}


class EmailSandboxAdapter(SandboxAdapter):
    channel = "email"


class MaxSandboxAdapter(SandboxAdapter):
    channel = "max"


class TelegramSandboxAdapter(SandboxAdapter):
    channel = "telegram"


def sandbox_adapter(channel: str) -> SandboxAdapter:
    adapters = {"email": EmailSandboxAdapter, "max": MaxSandboxAdapter, "telegram": TelegramSandboxAdapter}
    if channel not in adapters:
        raise ValueError("unsupported sandbox channel")
    return adapters[channel]()
