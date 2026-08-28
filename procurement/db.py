from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    region TEXT NOT NULL,
    cluster TEXT NOT NULL DEFAULT '',
    delivery_address TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'draft',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS project_sections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    code TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(project_id, code)
);

CREATE TABLE IF NOT EXISTS suppliers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    tax_id TEXT NOT NULL DEFAULT '',
    region TEXT NOT NULL,
    email TEXT NOT NULL DEFAULT '',
    phone TEXT NOT NULL DEFAULT '',
    telegram TEXT NOT NULL DEFAULT '',
    max_contact TEXT NOT NULL DEFAULT '',
    cluster TEXT NOT NULL DEFAULT '',
    categories_json TEXT NOT NULL DEFAULT '[]',
    rating REAL NOT NULL DEFAULT 3,
    verified INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    source TEXT NOT NULL DEFAULT 'manual',
    created_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_supplier_tax_id
ON suppliers(tax_id) WHERE tax_id != '';

CREATE TABLE IF NOT EXISTS lots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    section_id INTEGER REFERENCES project_sections(id) ON DELETE SET NULL,
    title TEXT NOT NULL,
    region TEXT NOT NULL,
    cluster TEXT NOT NULL DEFAULT '',
    delivery_address TEXT NOT NULL,
    response_deadline TEXT NOT NULL,
    desired_delivery_date TEXT,
    currency TEXT NOT NULL,
    rfq_requirements_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'draft',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS lot_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lot_id INTEGER NOT NULL REFERENCES lots(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    quantity TEXT NOT NULL,
    unit TEXT NOT NULL,
    specification TEXT NOT NULL DEFAULT '',
    source_document_id INTEGER REFERENCES source_documents(id) ON DELETE SET NULL,
    source_page INTEGER,
    source_reference TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS templates (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS campaigns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lot_id INTEGER NOT NULL REFERENCES lots(id) ON DELETE CASCADE,
    template_code TEXT NOT NULL REFERENCES templates(code),
    channel TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS outbox_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    supplier_id INTEGER NOT NULL REFERENCES suppliers(id),
    channel TEXT NOT NULL,
    recipient TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    approved_by TEXT NOT NULL DEFAULT '',
    approved_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(campaign_id, supplier_id)
);

CREATE TABLE IF NOT EXISTS quotes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lot_id INTEGER NOT NULL REFERENCES lots(id) ON DELETE CASCADE,
    supplier_id INTEGER NOT NULL REFERENCES suppliers(id),
    currency TEXT NOT NULL,
    vat_included INTEGER NOT NULL,
    delivery_cost TEXT NOT NULL,
    lead_days INTEGER NOT NULL,
    payment_terms TEXT NOT NULL DEFAULT '',
    warranty TEXT NOT NULL DEFAULT '',
    valid_until TEXT,
    source_filename TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS quote_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    quote_id INTEGER NOT NULL REFERENCES quotes(id) ON DELETE CASCADE,
    lot_item_id INTEGER NOT NULL REFERENCES lot_items(id),
    unit_price TEXT NOT NULL,
    offered_quantity TEXT,
    compliant INTEGER NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    UNIQUE(quote_id, lot_item_id)
);

CREATE TABLE IF NOT EXISTS purchase_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint TEXT NOT NULL UNIQUE,
    supplier_id INTEGER REFERENCES suppliers(id) ON DELETE SET NULL,
    source_document_id INTEGER REFERENCES source_documents(id) ON DELETE SET NULL,
    item_name TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    quantity TEXT NOT NULL,
    unit TEXT NOT NULL,
    unit_price TEXT NOT NULL,
    currency TEXT NOT NULL,
    vat_included INTEGER NOT NULL,
    purchased_on TEXT NOT NULL,
    invoice_number TEXT NOT NULL DEFAULT '',
    project_name TEXT NOT NULL DEFAULT '',
    region TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'manual',
    review_status TEXT NOT NULL DEFAULT 'approved',
    confirmed_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_purchase_history_lookup
ON purchase_history(normalized_name, unit, currency, purchased_on DESC);

CREATE TABLE IF NOT EXISTS source_documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL,
    supplier_id INTEGER REFERENCES suppliers(id) ON DELETE SET NULL,
    document_type TEXT NOT NULL,
    filename TEXT NOT NULL,
    content_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    sha256 TEXT NOT NULL UNIQUE,
    storage_path TEXT NOT NULL,
    extraction_status TEXT NOT NULL DEFAULT 'pending_ai_extraction',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mail_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT NOT NULL UNIQUE,
    mailbox_address TEXT NOT NULL,
    direction TEXT NOT NULL,
    sender TEXT NOT NULL,
    recipients_json TEXT NOT NULL DEFAULT '[]',
    cc_json TEXT NOT NULL DEFAULT '[]',
    subject TEXT NOT NULL DEFAULT '',
    body_text TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    thread_key TEXT NOT NULL,
    in_reply_to TEXT NOT NULL DEFAULT '',
    references_json TEXT NOT NULL DEFAULT '[]',
    imap_uid INTEGER,
    imap_uidvalidity TEXT NOT NULL DEFAULT '',
    supplier_id INTEGER REFERENCES suppliers(id) ON DELETE SET NULL,
    project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL,
    lot_id INTEGER REFERENCES lots(id) ON DELETE SET NULL,
    outbox_message_id INTEGER UNIQUE REFERENCES outbox_messages(id) ON DELETE SET NULL,
    reply_to_message_id INTEGER REFERENCES mail_messages(id) ON DELETE SET NULL,
    approved_by TEXT NOT NULL DEFAULT '',
    approved_at TEXT,
    sent_at TEXT,
    received_at TEXT,
    last_error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_mail_messages_folder
ON mail_messages(direction, status, received_at DESC, created_at DESC);

CREATE INDEX IF NOT EXISTS ix_mail_messages_links
ON mail_messages(lot_id, supplier_id, project_id);

CREATE TABLE IF NOT EXISTS mail_attachments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mail_message_id INTEGER NOT NULL REFERENCES mail_messages(id) ON DELETE CASCADE,
    source_document_id INTEGER REFERENCES source_documents(id) ON DELETE SET NULL,
    filename TEXT NOT NULL,
    content_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    blocked_reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(mail_message_id, sha256, filename)
);

CREATE TABLE IF NOT EXISTS mailbox_sync_state (
    mailbox_address TEXT PRIMARY KEY,
    uidvalidity TEXT NOT NULL DEFAULT '',
    last_uid INTEGER NOT NULL DEFAULT 0,
    last_synced_at TEXT,
    last_error TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS procurement_suggestions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_document_id INTEGER NOT NULL REFERENCES source_documents(id) ON DELETE CASCADE,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    section_code TEXT NOT NULL,
    section_name TEXT NOT NULL,
    lot_title TEXT NOT NULL,
    items_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '[]',
    confidence REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'needs_review',
    reviewed_by TEXT NOT NULL DEFAULT '',
    reviewed_at TEXT,
    lot_id INTEGER REFERENCES lots(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    UNIQUE(source_document_id, section_code, lot_title)
);

CREATE INDEX IF NOT EXISTS ix_procurement_suggestions_review
ON procurement_suggestions(status, project_id, created_at DESC);

CREATE TABLE IF NOT EXISTS document_reference_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    suggestion_id INTEGER NOT NULL REFERENCES procurement_suggestions(id) ON DELETE CASCADE,
    reference_document_id INTEGER NOT NULL REFERENCES source_documents(id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(suggestion_id, reference_document_id)
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
"""


DEFAULT_TEMPLATES = {
    "rfq-email": {
        "name": "Запрос коммерческого предложения — email",
        "subject": "Запрос КП: {lot_title} — ответ до {response_deadline}",
        "body": (
            "Здравствуйте, {supplier_name}!\n\n"
            "Просим предоставить коммерческое предложение по заявке «{lot_title}».\n\n"
            "Объект: {project_name}\n"
            "Регион: {region}\n"
            "Адрес поставки: {delivery_address}\n"
            "Желаемый срок поставки: {desired_delivery_date}\n\n"
            "Спецификация:\n{items}\n\n"
            "Просим отдельно указать цену, НДС, стоимость доставки, срок поставки, "
            "условия оплаты, гарантию и срок действия предложения.\n"
            "Ответ ожидаем до {response_deadline}.\n\n"
            "С уважением,\nОтдел снабжения"
        ),
    },
    "rfq-messenger": {
        "name": "Короткий запрос КП — мессенджер",
        "subject": "Запрос КП: {lot_title}",
        "body": (
            "Здравствуйте! Запрашиваем КП по позиции «{lot_title}» для объекта "
            "{project_name}, доставка: {delivery_address}.\n{items}\n"
            "Нужны цена с НДС, доставка, срок и условия оплаты. Ответ до {response_deadline}."
        ),
    },
}


def utcnow() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


class Database:
    def __init__(self, path: str):
        self.path = path

    def initialize(self) -> None:
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as conn:
            conn.executescript(SCHEMA)
            self._migrate_columns(conn)
            for code, template in DEFAULT_TEMPLATES.items():
                conn.execute(
                    """
                    INSERT INTO templates(code, name, subject, body, version, updated_at)
                    VALUES (?, ?, ?, ?, 1, ?)
                    ON CONFLICT(code) DO NOTHING
                    """,
                    (code, template["name"], template["subject"], template["body"], utcnow()),
                )

    @staticmethod
    def _migrate_columns(conn: sqlite3.Connection) -> None:
        additions = {
            "projects": (
                ("cluster", "TEXT NOT NULL DEFAULT ''"),
            ),
            "suppliers": (
                ("max_contact", "TEXT NOT NULL DEFAULT ''"),
                ("cluster", "TEXT NOT NULL DEFAULT ''"),
            ),
            "lots": (
                ("cluster", "TEXT NOT NULL DEFAULT ''"),
                ("rfq_requirements_json", "TEXT NOT NULL DEFAULT '{}'"),
            ),
            "lot_items": (
                ("source_document_id", "INTEGER REFERENCES source_documents(id) ON DELETE SET NULL"),
                ("source_page", "INTEGER"),
                ("source_reference", "TEXT NOT NULL DEFAULT ''"),
            ),
        }
        for table, columns in additions.items():
            existing = {
                str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            for name, definition in columns:
                if name not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        if self.path != ":memory:":
            conn.execute("PRAGMA journal_mode = WAL")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def one(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def all(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def audit(
        self,
        action: str,
        entity_type: str,
        entity_id: str | int,
        *,
        actor: str = "system",
        details: dict[str, Any] | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        values = (
            actor,
            action,
            entity_type,
            str(entity_id),
            json.dumps(details or {}, ensure_ascii=False, default=str),
            utcnow(),
        )
        if conn is not None:
            conn.execute(
                "INSERT INTO audit_log(actor, action, entity_type, entity_id, details_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                values,
            )
            return
        with self.connection() as owned_conn:
            owned_conn.execute(
                "INSERT INTO audit_log(actor, action, entity_type, entity_id, details_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                values,
            )
