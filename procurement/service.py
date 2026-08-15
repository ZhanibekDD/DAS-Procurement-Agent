from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from .db import Database, utcnow
from .models import (
    CampaignCreate,
    LotCreate,
    ProjectCreate,
    QuoteCreate,
    SectionCreate,
    SupplierCreate,
    TemplateUpsert,
)
from .ranking import rank_quotes
from .templates import render_template


class NotFoundError(ValueError):
    pass


class ConflictError(ValueError):
    pass


class ProcurementService:
    def __init__(self, db: Database):
        self.db = db

    def create_project(self, data: ProjectCreate) -> dict[str, Any]:
        with self.db.connection() as conn:
            cursor = conn.execute(
                "INSERT INTO projects(name, region, delivery_address, description, created_at) VALUES (?, ?, ?, ?, ?)",
                (data.name, data.region, data.delivery_address, data.description, utcnow()),
            )
            project_id = cursor.lastrowid
            self.db.audit("created", "project", project_id, conn=conn)
        return self.get_project(project_id)

    def get_project(self, project_id: int) -> dict[str, Any]:
        row = self.db.one("SELECT * FROM projects WHERE id = ?", (project_id,))
        if not row:
            raise NotFoundError("project not found")
        row["sections"] = self.db.all(
            "SELECT * FROM project_sections WHERE project_id = ? ORDER BY code", (project_id,)
        )
        row["lots"] = self.db.all(
            "SELECT * FROM lots WHERE project_id = ? ORDER BY id DESC", (project_id,)
        )
        return row

    def list_projects(self) -> list[dict[str, Any]]:
        return self.db.all("SELECT * FROM projects ORDER BY id DESC")

    def add_section(self, project_id: int, data: SectionCreate) -> dict[str, Any]:
        self.get_project(project_id)
        with self.db.connection() as conn:
            cursor = conn.execute(
                "INSERT INTO project_sections(project_id, code, name, description, created_at) VALUES (?, ?, ?, ?, ?)",
                (project_id, data.code, data.name, data.description, utcnow()),
            )
            section_id = cursor.lastrowid
            self.db.audit("created", "project_section", section_id, conn=conn)
        return self.db.one("SELECT * FROM project_sections WHERE id = ?", (section_id,)) or {}

    def create_supplier(self, data: SupplierCreate, *, source: str = "manual") -> dict[str, Any]:
        with self.db.connection() as conn:
            try:
                cursor = conn.execute(
                    """
                    INSERT INTO suppliers(
                        name, tax_id, region, email, phone, telegram, categories_json,
                        rating, verified, source, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        data.name,
                        data.tax_id,
                        data.region,
                        data.email,
                        data.phone,
                        data.telegram,
                        json.dumps(data.categories, ensure_ascii=False),
                        data.rating,
                        int(data.verified),
                        source,
                        utcnow(),
                    ),
                )
            except Exception as exc:
                if "UNIQUE constraint" in str(exc):
                    raise ConflictError("supplier with this tax_id already exists") from exc
                raise
            supplier_id = cursor.lastrowid
            self.db.audit("created", "supplier", supplier_id, details={"source": source}, conn=conn)
        return self.get_supplier(supplier_id)

    def get_supplier(self, supplier_id: int) -> dict[str, Any]:
        row = self.db.one("SELECT * FROM suppliers WHERE id = ?", (supplier_id,))
        if not row:
            raise NotFoundError("supplier not found")
        row["categories"] = json.loads(row.pop("categories_json"))
        row["verified"] = bool(row["verified"])
        row["active"] = bool(row["active"])
        return row

    def list_suppliers(self, region: str = "", category: str = "") -> list[dict[str, Any]]:
        rows = self.db.all("SELECT * FROM suppliers WHERE active = 1 ORDER BY verified DESC, rating DESC, name")
        result = []
        for row in rows:
            row["categories"] = json.loads(row.pop("categories_json"))
            if region and region.casefold() not in row["region"].casefold():
                continue
            if category and not any(category.casefold() in value.casefold() for value in row["categories"]):
                continue
            row["verified"] = bool(row["verified"])
            row["active"] = bool(row["active"])
            result.append(row)
        return result

    def create_lot(self, data: LotCreate) -> dict[str, Any]:
        project = self.get_project(data.project_id)
        if data.section_id is not None:
            section = self.db.one(
                "SELECT id FROM project_sections WHERE id = ? AND project_id = ?",
                (data.section_id, data.project_id),
            )
            if not section:
                raise NotFoundError("project section not found")
        with self.db.connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO lots(
                    project_id, section_id, title, region, delivery_address,
                    response_deadline, desired_delivery_date, currency, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    data.project_id,
                    data.section_id,
                    data.title,
                    data.region,
                    data.delivery_address,
                    data.response_deadline.isoformat(),
                    data.desired_delivery_date.isoformat() if data.desired_delivery_date else None,
                    data.currency,
                    utcnow(),
                ),
            )
            lot_id = cursor.lastrowid
            for item in data.items:
                conn.execute(
                    "INSERT INTO lot_items(lot_id, name, quantity, unit, specification) VALUES (?, ?, ?, ?, ?)",
                    (lot_id, item.name, str(item.quantity), item.unit, item.specification),
                )
            self.db.audit(
                "created", "lot", lot_id, details={"project_name": project["name"]}, conn=conn
            )
        return self.get_lot(lot_id)

    def get_lot(self, lot_id: int) -> dict[str, Any]:
        row = self.db.one("SELECT * FROM lots WHERE id = ?", (lot_id,))
        if not row:
            raise NotFoundError("lot not found")
        row["items"] = self.db.all("SELECT * FROM lot_items WHERE lot_id = ? ORDER BY id", (lot_id,))
        return row

    def list_lots(self) -> list[dict[str, Any]]:
        return self.db.all(
            """
            SELECT lots.*, projects.name AS project_name
            FROM lots JOIN projects ON projects.id = lots.project_id
            ORDER BY lots.id DESC
            """
        )

    def match_suppliers(self, lot_id: int) -> list[dict[str, Any]]:
        lot = self.get_lot(lot_id)
        search_text = " ".join([lot["title"], *(item["name"] for item in lot["items"])]).casefold()
        candidates = []
        for supplier in self.list_suppliers():
            region_match = lot["region"].casefold() in supplier["region"].casefold() or supplier[
                "region"
            ].casefold() in lot["region"].casefold()
            category_hits = sum(
                1 for category in supplier["categories"] if category.casefold() in search_text
            )
            score = category_hits * 40 + int(region_match) * 25 + int(supplier["verified"]) * 20 + supplier[
                "rating"
            ] * 3
            if score > 0:
                supplier["match_score"] = round(score, 2)
                supplier["match_reasons"] = {
                    "region": region_match,
                    "category_hits": category_hits,
                    "verified": supplier["verified"],
                }
                candidates.append(supplier)
        return sorted(candidates, key=lambda row: (-row["match_score"], row["name"]))

    def upsert_template(self, code: str, data: TemplateUpsert) -> dict[str, Any]:
        with self.db.connection() as conn:
            conn.execute(
                """
                INSERT INTO templates(code, name, subject, body, version, updated_at)
                VALUES (?, ?, ?, ?, 1, ?)
                ON CONFLICT(code) DO UPDATE SET
                    name=excluded.name, subject=excluded.subject, body=excluded.body,
                    version=templates.version+1, updated_at=excluded.updated_at
                """,
                (code, data.name, data.subject, data.body, utcnow()),
            )
            self.db.audit("upserted", "template", code, conn=conn)
        return self.db.one("SELECT * FROM templates WHERE code = ?", (code,)) or {}

    def list_templates(self) -> list[dict[str, Any]]:
        return self.db.all("SELECT * FROM templates ORDER BY code")

    def create_campaign(self, lot_id: int, data: CampaignCreate) -> dict[str, Any]:
        lot = self.get_lot(lot_id)
        project = self.get_project(lot["project_id"])
        template = self.db.one("SELECT * FROM templates WHERE code = ?", (data.template_code,))
        if not template:
            raise NotFoundError("template not found")
        suppliers = [self.get_supplier(supplier_id) for supplier_id in dict.fromkeys(data.supplier_ids)]
        items_text = "\n".join(
            f"- {item['name']}: {item['quantity']} {item['unit']}"
            + (f"; {item['specification']}" if item["specification"] else "")
            for item in lot["items"]
        )
        prepared: list[tuple[dict[str, Any], str, str, str]] = []
        for supplier in suppliers:
            recipient = {
                "email": supplier["email"],
                "telegram": supplier["telegram"],
                "whatsapp": supplier["phone"],
            }[data.channel]
            if not recipient:
                raise ValueError(f"supplier {supplier['id']} has no {data.channel} contact")
            context = {
                "supplier_name": supplier["name"],
                "lot_title": lot["title"],
                "project_name": project["name"],
                "region": lot["region"],
                "delivery_address": lot["delivery_address"],
                "desired_delivery_date": lot["desired_delivery_date"] or "по согласованию",
                "response_deadline": lot["response_deadline"],
                "items": items_text,
            }
            prepared.append(
                (
                    supplier,
                    recipient,
                    render_template(template["subject"], context),
                    render_template(template["body"], context),
                )
            )
        with self.db.connection() as conn:
            cursor = conn.execute(
                "INSERT INTO campaigns(lot_id, template_code, channel, created_at) VALUES (?, ?, ?, ?)",
                (lot_id, data.template_code, data.channel, utcnow()),
            )
            campaign_id = cursor.lastrowid
            for supplier, recipient, subject, body in prepared:
                conn.execute(
                    """
                    INSERT INTO outbox_messages(
                        campaign_id, supplier_id, channel, recipient, subject, body, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (campaign_id, supplier["id"], data.channel, recipient, subject, body, utcnow()),
                )
            conn.execute("UPDATE lots SET status = 'rfq_draft' WHERE id = ?", (lot_id,))
            self.db.audit(
                "drafted",
                "campaign",
                campaign_id,
                details={"message_count": len(prepared), "channel": data.channel},
                conn=conn,
            )
        return self.get_campaign(campaign_id)

    def get_campaign(self, campaign_id: int) -> dict[str, Any]:
        row = self.db.one("SELECT * FROM campaigns WHERE id = ?", (campaign_id,))
        if not row:
            raise NotFoundError("campaign not found")
        row["messages"] = self.db.all(
            """
            SELECT outbox_messages.*, suppliers.name AS supplier_name
            FROM outbox_messages JOIN suppliers ON suppliers.id = outbox_messages.supplier_id
            WHERE campaign_id = ? ORDER BY outbox_messages.id
            """,
            (campaign_id,),
        )
        return row

    def approve_message(self, message_id: int, approved_by: str, comment: str = "") -> dict[str, Any]:
        message = self.db.one("SELECT * FROM outbox_messages WHERE id = ?", (message_id,))
        if not message:
            raise NotFoundError("outbox message not found")
        if message["status"] != "draft":
            raise ConflictError("only draft messages can be approved")
        with self.db.connection() as conn:
            conn.execute(
                "UPDATE outbox_messages SET status='approved', approved_by=?, approved_at=? WHERE id=?",
                (approved_by, utcnow(), message_id),
            )
            self.db.audit(
                "approved",
                "outbox_message",
                message_id,
                actor=approved_by,
                details={"comment": comment, "dispatch": "disabled_in_mvp"},
                conn=conn,
            )
        return self.db.one("SELECT * FROM outbox_messages WHERE id = ?", (message_id,)) or {}

    def add_quote(self, lot_id: int, data: QuoteCreate) -> dict[str, Any]:
        lot = self.get_lot(lot_id)
        self.get_supplier(data.supplier_id)
        lot_item_ids = {int(item["id"]) for item in lot["items"]}
        submitted_ids = {item.lot_item_id for item in data.items}
        if not submitted_ids.issubset(lot_item_ids):
            raise ValueError("quote contains an item from another lot")
        with self.db.connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO quotes(
                    lot_id, supplier_id, currency, vat_included, delivery_cost,
                    lead_days, payment_terms, warranty, valid_until, source_filename, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    lot_id,
                    data.supplier_id,
                    data.currency,
                    int(data.vat_included),
                    str(data.delivery_cost),
                    data.lead_days,
                    data.payment_terms,
                    data.warranty,
                    data.valid_until.isoformat() if data.valid_until else None,
                    data.source_filename,
                    utcnow(),
                ),
            )
            quote_id = cursor.lastrowid
            for item in data.items:
                conn.execute(
                    """
                    INSERT INTO quote_items(
                        quote_id, lot_item_id, unit_price, offered_quantity, compliant, note
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        quote_id,
                        item.lot_item_id,
                        str(item.unit_price),
                        str(item.offered_quantity) if item.offered_quantity is not None else None,
                        int(item.compliant),
                        item.note,
                    ),
                )
            conn.execute("UPDATE lots SET status = 'quotes_received' WHERE id = ?", (lot_id,))
            self.db.audit("received", "quote", quote_id, details={"lot_id": lot_id}, conn=conn)
        return self.db.one("SELECT * FROM quotes WHERE id = ?", (quote_id,)) or {}

    def comparison(self, lot_id: int) -> dict[str, Any]:
        lot = self.get_lot(lot_id)
        requested = {int(item["id"]): Decimal(item["quantity"]) for item in lot["items"]}
        quotes = self.db.all(
            """
            SELECT quotes.*, suppliers.name AS supplier_name, suppliers.rating AS supplier_rating
            FROM quotes JOIN suppliers ON suppliers.id = quotes.supplier_id
            WHERE quotes.lot_id = ? ORDER BY quotes.id
            """,
            (lot_id,),
        )
        rows = []
        for quote in quotes:
            items = self.db.all("SELECT * FROM quote_items WHERE quote_id = ?", (quote["id"],))
            subtotal = sum(
                requested[int(item["lot_item_id"])] * Decimal(item["unit_price"]) for item in items
            )
            complete = {int(item["lot_item_id"]) for item in items} == set(requested)
            compliant = complete and all(bool(item["compliant"]) for item in items)
            total = subtotal + Decimal(quote["delivery_cost"])
            rows.append(
                {
                    "quote_id": quote["id"],
                    "supplier_id": quote["supplier_id"],
                    "supplier_name": quote["supplier_name"],
                    "supplier_rating": quote["supplier_rating"],
                    "currency": quote["currency"],
                    "subtotal": float(subtotal),
                    "delivery_cost": float(Decimal(quote["delivery_cost"])),
                    "total_cost": float(total),
                    "lead_days": quote["lead_days"],
                    "payment_terms": quote["payment_terms"],
                    "warranty": quote["warranty"],
                    "vat_included": bool(quote["vat_included"]),
                    "compliant": compliant,
                    "coverage": f"{len(items)}/{len(requested)}",
                }
            )
        return {
            "lot": lot,
            "ranking_policy": "landed_cost_60_delivery_15_reliability_15_terms_10",
            "quotes": rank_quotes(rows),
            "decision": "human_approval_required",
        }

    def register_source_document(
        self,
        *,
        filename: str,
        content: bytes,
        document_type: str,
        content_type: str = "application/octet-stream",
        project_id: int | None = None,
        supplier_id: int | None = None,
    ) -> dict[str, Any]:
        allowed_types = {"paid_invoice", "tender_table", "project_section", "commercial_offer"}
        if document_type not in allowed_types:
            raise ValueError("unsupported document type")
        if not content or len(content) > 25 * 1024 * 1024:
            raise ValueError("document must be between 1 byte and 25 MB")
        suffix = Path(filename).suffix.lower()
        if suffix not in {".pdf", ".xlsx", ".csv", ".docx"}:
            raise ValueError("only PDF, DOCX, XLSX and CSV documents are supported")
        if suffix == ".pdf" and not content.startswith(b"%PDF-"):
            raise ValueError("invalid PDF payload")
        if suffix in {".xlsx", ".docx"} and not content.startswith(b"PK"):
            raise ValueError("invalid Office document payload")
        if project_id is not None:
            self.get_project(project_id)
        if supplier_id is not None:
            self.get_supplier(supplier_id)

        digest = hashlib.sha256(content).hexdigest()
        existing = self.db.one("SELECT * FROM source_documents WHERE sha256 = ?", (digest,))
        if existing:
            return existing
        if self.db.path == ":memory:":
            raise RuntimeError("document storage is unavailable for in-memory database")

        storage_dir = Path(self.db.path).resolve().parent / "uploads" / document_type
        storage_dir.mkdir(parents=True, exist_ok=True)
        storage_path = storage_dir / f"{digest}{suffix}"
        storage_path.write_bytes(content)
        with self.db.connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO source_documents(
                    project_id, supplier_id, document_type, filename, content_type,
                    size_bytes, sha256, storage_path, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    project_id,
                    supplier_id,
                    document_type,
                    Path(filename).name,
                    content_type,
                    len(content),
                    digest,
                    str(storage_path),
                    utcnow(),
                ),
            )
            document_id = cursor.lastrowid
            self.db.audit(
                "uploaded",
                "source_document",
                document_id,
                details={"type": document_type, "sha256_prefix": digest[:12]},
                conn=conn,
            )
        return self.db.one("SELECT * FROM source_documents WHERE id = ?", (document_id,)) or {}

    def list_source_documents(self, extraction_status: str = "") -> list[dict[str, Any]]:
        if extraction_status:
            return self.db.all(
                "SELECT * FROM source_documents WHERE extraction_status = ? ORDER BY id DESC",
                (extraction_status,),
            )
        return self.db.all("SELECT * FROM source_documents ORDER BY id DESC")

    def dashboard(self) -> dict[str, int]:
        return {
            "projects": int((self.db.one("SELECT COUNT(*) AS n FROM projects") or {"n": 0})["n"]),
            "suppliers": int((self.db.one("SELECT COUNT(*) AS n FROM suppliers") or {"n": 0})["n"]),
            "active_lots": int(
                (
                    self.db.one(
                        "SELECT COUNT(*) AS n FROM lots WHERE status NOT IN ('awarded', 'cancelled')"
                    )
                    or {"n": 0}
                )["n"]
            ),
            "draft_messages": int(
                (
                    self.db.one("SELECT COUNT(*) AS n FROM outbox_messages WHERE status='draft'")
                    or {"n": 0}
                )["n"]
            ),
            "received_quotes": int((self.db.one("SELECT COUNT(*) AS n FROM quotes") or {"n": 0})["n"]),
            "pending_documents": int(
                (
                    self.db.one(
                        "SELECT COUNT(*) AS n FROM source_documents WHERE extraction_status='pending_ai_extraction'"
                    )
                    or {"n": 0}
                )["n"]
            ),
        }
