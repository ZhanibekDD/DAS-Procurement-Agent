from __future__ import annotations

import hashlib
import json
import re
from difflib import SequenceMatcher
from decimal import Decimal
from email.utils import make_msgid
from pathlib import Path
from statistics import median
from typing import Any

from .db import Database, utcnow
from .document_analysis import (
    compare_fence_documents,
    extract_bulat_fence_schedule,
    extract_bulat_invoice,
    extract_pdf_page,
    read_stored_pdf,
)
from .models import (
    CampaignCreate,
    LotCreate,
    LotItemCreate,
    MailDraftCreate,
    MailLinkUpdate,
    ProcurementSuggestionApproval,
    ProcurementSuggestionCreate,
    ProcurementSuggestionRejection,
    PurchaseHistoryCreate,
    ProjectCreate,
    QuoteCreate,
    SectionCreate,
    SupplierCreate,
    TemplateUpsert,
)
from .mailbox import InboundMail
from .ranking import rank_quotes
from .regions import resolve_cluster
from .templates import render_template


class NotFoundError(ValueError):
    pass


class ConflictError(ValueError):
    pass


class ProcurementService:
    def __init__(self, db: Database):
        self.db = db

    def create_project(self, data: ProjectCreate) -> dict[str, Any]:
        cluster = resolve_cluster(data.region, data.cluster)
        with self.db.connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO projects(
                    name, region, cluster, delivery_address, description, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    data.name,
                    data.region,
                    cluster,
                    data.delivery_address,
                    data.description,
                    utcnow(),
                ),
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
        cluster = resolve_cluster(data.region, data.cluster)
        with self.db.connection() as conn:
            try:
                cursor = conn.execute(
                    """
                    INSERT INTO suppliers(
                        name, tax_id, region, email, phone, telegram, max_contact, cluster,
                        categories_json, rating, verified, source, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        data.name,
                        data.tax_id,
                        data.region,
                        data.email,
                        data.phone,
                        data.telegram,
                        data.max_contact,
                        cluster,
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
        cluster = resolve_cluster(data.region, data.cluster or project["cluster"])
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
                    project_id, section_id, title, region, cluster, delivery_address,
                    response_deadline, desired_delivery_date, currency,
                    rfq_requirements_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    data.project_id,
                    data.section_id,
                    data.title,
                    data.region,
                    cluster,
                    data.delivery_address,
                    data.response_deadline.isoformat(),
                    data.desired_delivery_date.isoformat() if data.desired_delivery_date else None,
                    data.currency,
                    json.dumps(
                        data.rfq_requirements.model_dump(mode="json")
                        if data.rfq_requirements
                        else {},
                        ensure_ascii=False,
                    ),
                    utcnow(),
                ),
            )
            lot_id = cursor.lastrowid
            for item in data.items:
                conn.execute(
                    """
                    INSERT INTO lot_items(
                        lot_id, name, quantity, unit, specification,
                        source_document_id, source_page, source_reference
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        lot_id,
                        item.name,
                        str(item.quantity),
                        item.unit,
                        item.specification,
                        item.source_document_id,
                        item.source_page,
                        item.source_reference,
                    ),
                )
            self.db.audit(
                "created", "lot", lot_id, details={"project_name": project["name"]}, conn=conn
            )
        return self.get_lot(lot_id)

    def get_lot(self, lot_id: int) -> dict[str, Any]:
        row = self.db.one("SELECT * FROM lots WHERE id = ?", (lot_id,))
        if not row:
            raise NotFoundError("lot not found")
        row["rfq_requirements"] = json.loads(row.pop("rfq_requirements_json", "{}") or "{}")
        row["items"] = self.db.all("SELECT * FROM lot_items WHERE lot_id = ? ORDER BY id", (lot_id,))
        return row

    def list_lots(self) -> list[dict[str, Any]]:
        rows = self.db.all(
            """
            SELECT lots.*, projects.name AS project_name
            FROM lots JOIN projects ON projects.id = lots.project_id
            ORDER BY lots.id DESC
            """
        )
        for row in rows:
            row["rfq_requirements"] = json.loads(
                row.pop("rfq_requirements_json", "{}") or "{}"
            )
        return rows

    def match_suppliers(self, lot_id: int) -> list[dict[str, Any]]:
        lot = self.get_lot(lot_id)
        search_text = " ".join([lot["title"], *(item["name"] for item in lot["items"])]).casefold()
        candidates = []
        for supplier in self.list_suppliers():
            if lot["cluster"] and supplier["cluster"] != lot["cluster"]:
                continue
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
                    "cluster": lot["cluster"] or "legacy_unassigned",
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
        if lot["cluster"]:
            invalid = [
                supplier["id"]
                for supplier in suppliers
                if supplier["cluster"] != lot["cluster"]
            ]
            if invalid:
                raise ValueError(
                    "supplier cluster must match lot cluster; blocked supplier ids: "
                    + ", ".join(str(value) for value in invalid)
                )
        items_text = "\n".join(
            f"- {item['name']}: {item['quantity']} {item['unit']}"
            + (f"; {item['specification']}" if item["specification"] else "")
            for item in lot["items"]
        )
        requirements = lot.get("rfq_requirements") or {}
        requirement_labels = {
            "delivery_address_confirmation": "Подтверждение адреса доставки",
            "coating": "Покрытие",
            "color_ral": "Цвет RAL",
            "mesh_cell": "Ячейка сетки",
            "rod_diameter": "Диаметр прутка",
            "delivery_or_pickup": "Логистика",
        }
        requirement_values = {
            "delivery": "доставка поставщиком",
            "pickup": "самовывоз",
            "supplier_choice": "указать оба варианта",
        }
        requirements_text = "\n".join(
            f"- {requirement_labels[key]}: {requirement_values.get(str(value), value)}"
            for key, value in requirements.items()
            if value and key in requirement_labels
        )
        if requirements_text:
            items_text += "\n\nДополнительные требования:\n" + requirements_text
        prepared: list[tuple[dict[str, Any], str, str, str]] = []
        for supplier in suppliers:
            recipient = {
                "email": supplier["email"],
                "telegram": supplier["telegram"],
                "max": supplier["max_contact"],
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

    def list_campaigns(self, lot_id: int | None = None) -> list[dict[str, Any]]:
        params: tuple[Any, ...] = ()
        where = ""
        if lot_id is not None:
            self.get_lot(lot_id)
            where = "WHERE campaigns.lot_id = ?"
            params = (lot_id,)
        return self.db.all(
            f"""
            SELECT campaigns.*, lots.title AS lot_title,
                   COUNT(outbox_messages.id) AS message_count,
                   SUM(CASE WHEN outbox_messages.status = 'approved' THEN 1 ELSE 0 END) AS approved_count
            FROM campaigns
            JOIN lots ON lots.id = campaigns.lot_id
            LEFT JOIN outbox_messages ON outbox_messages.campaign_id = campaigns.id
            {where}
            GROUP BY campaigns.id
            ORDER BY campaigns.id DESC
            """,
            params,
        )

    def list_outbox(self, status: str = "", lot_id: int | None = None) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            if status not in {"draft", "approved", "sent", "failed"}:
                raise ValueError("unsupported outbox status")
            clauses.append("outbox_messages.status = ?")
            params.append(status)
        if lot_id is not None:
            self.get_lot(lot_id)
            clauses.append("campaigns.lot_id = ?")
            params.append(lot_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return self.db.all(
            f"""
            SELECT outbox_messages.*, suppliers.name AS supplier_name,
                   campaigns.lot_id, lots.title AS lot_title
            FROM outbox_messages
            JOIN suppliers ON suppliers.id = outbox_messages.supplier_id
            JOIN campaigns ON campaigns.id = outbox_messages.campaign_id
            JOIN lots ON lots.id = campaigns.lot_id
            {where}
            ORDER BY outbox_messages.id DESC
            """,
            tuple(params),
        )

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

    @staticmethod
    def _normalized_item_name(value: str) -> str:
        return " ".join(re.findall(r"[0-9a-zа-я]+", value.casefold().replace("ё", "е")))

    @classmethod
    def _item_match_score(cls, requested: str, historical: str) -> float:
        left = cls._normalized_item_name(requested)
        right = cls._normalized_item_name(historical)
        if not left or not right:
            return 0.0
        if left == right:
            return 1.0
        left_tokens, right_tokens = set(left.split()), set(right.split())
        token_score = len(left_tokens & right_tokens) / len(left_tokens | right_tokens)
        containment = 0.9 if left_tokens <= right_tokens or right_tokens <= left_tokens else 0.0
        return max(token_score, containment, SequenceMatcher(None, left, right).ratio())

    def add_purchase_history(self, data: PurchaseHistoryCreate) -> dict[str, Any]:
        if data.supplier_id is not None:
            self.get_supplier(data.supplier_id)
        if data.source_document_id is not None:
            document = self.db.one(
                "SELECT id, document_type FROM source_documents WHERE id = ?",
                (data.source_document_id,),
            )
            if not document:
                raise NotFoundError("source document not found")
            if document["document_type"] != "paid_invoice":
                raise ValueError("price history source must be a paid invoice")
        normalized_name = self._normalized_item_name(data.item_name)
        fingerprint_source = "|".join(
            [
                str(data.supplier_id or 0),
                str(data.source_document_id or 0),
                normalized_name,
                str(data.quantity.normalize()),
                self._normalized_item_name(data.unit),
                str(data.unit_price.normalize()),
                data.currency,
                data.purchased_on.isoformat(),
                data.invoice_number.casefold(),
            ]
        )
        fingerprint = hashlib.sha256(fingerprint_source.encode("utf-8")).hexdigest()
        with self.db.connection() as conn:
            try:
                cursor = conn.execute(
                    """
                    INSERT INTO purchase_history(
                        fingerprint, supplier_id, source_document_id, item_name, normalized_name,
                        quantity, unit, unit_price, currency, vat_included, purchased_on,
                        invoice_number, project_name, region, source, review_status,
                        confirmed_by, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'manual', 'approved', ?, ?)
                    """,
                    (
                        fingerprint,
                        data.supplier_id,
                        data.source_document_id,
                        data.item_name,
                        normalized_name,
                        str(data.quantity),
                        data.unit,
                        str(data.unit_price),
                        data.currency,
                        int(data.vat_included),
                        data.purchased_on.isoformat(),
                        data.invoice_number,
                        data.project_name,
                        data.region,
                        data.confirmed_by,
                        utcnow(),
                    ),
                )
            except Exception as exc:
                if "UNIQUE constraint" in str(exc):
                    raise ConflictError("this paid purchase is already in price history") from exc
                raise
            record_id = cursor.lastrowid
            self.db.audit(
                "confirmed",
                "purchase_history",
                record_id,
                actor=data.confirmed_by,
                details={"source": "manual", "currency": data.currency},
                conn=conn,
            )
        return self.get_purchase_history(record_id)

    def get_purchase_history(self, record_id: int) -> dict[str, Any]:
        row = self.db.one(
            """
            SELECT purchase_history.*, suppliers.name AS supplier_name
            FROM purchase_history
            LEFT JOIN suppliers ON suppliers.id = purchase_history.supplier_id
            WHERE purchase_history.id = ?
            """,
            (record_id,),
        )
        if not row:
            raise NotFoundError("purchase history record not found")
        row["vat_included"] = bool(row["vat_included"])
        return row

    def list_purchase_history(
        self,
        *,
        search: str = "",
        supplier_id: int | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        if limit < 1 or limit > 500:
            raise ValueError("price history limit must be between 1 and 500")
        clauses = ["purchase_history.review_status = 'approved'"]
        params: list[Any] = []
        if supplier_id is not None:
            clauses.append("purchase_history.supplier_id = ?")
            params.append(supplier_id)
        if search:
            clauses.append("purchase_history.normalized_name LIKE ?")
            params.append(f"%{self._normalized_item_name(search)}%")
        params.append(limit)
        rows = self.db.all(
            f"""
            SELECT purchase_history.*, suppliers.name AS supplier_name
            FROM purchase_history
            LEFT JOIN suppliers ON suppliers.id = purchase_history.supplier_id
            WHERE {' AND '.join(clauses)}
            ORDER BY purchase_history.purchased_on DESC, purchase_history.id DESC
            LIMIT ?
            """,
            tuple(params),
        )
        for row in rows:
            row["vat_included"] = bool(row["vat_included"])
        return rows

    def lot_price_benchmark(self, lot_id: int) -> dict[str, Any]:
        lot = self.get_lot(lot_id)
        history = self.list_purchase_history(limit=500)
        items: list[dict[str, Any]] = []
        for item in lot["items"]:
            candidates = []
            for record in history:
                if record["currency"] != lot["currency"]:
                    continue
                if self._normalized_item_name(record["unit"]) != self._normalized_item_name(
                    item["unit"]
                ):
                    continue
                score = self._item_match_score(item["name"], record["item_name"])
                if score >= 0.75:
                    candidates.append((score, record))
            prices = [Decimal(record["unit_price"]) for _, record in candidates]
            items.append(
                {
                    "lot_item_id": item["id"],
                    "item_name": item["name"],
                    "unit": item["unit"],
                    "currency": lot["currency"],
                    "history_count": len(prices),
                    "median_unit_price": float(median(prices)) if prices else None,
                    "min_unit_price": float(min(prices)) if prices else None,
                    "max_unit_price": float(max(prices)) if prices else None,
                    "latest_unit_price": (
                        float(Decimal(candidates[0][1]["unit_price"])) if candidates else None
                    ),
                    "match_confidence": round(max((score for score, _ in candidates), default=0.0), 3),
                }
            )
        return {
            "lot_id": lot_id,
            "currency": lot["currency"],
            "matched_items": sum(1 for item in items if item["history_count"]),
            "total_items": len(items),
            "items": items,
            "policy": "approved_paid_invoices_same_currency_unit_match_gte_0_75",
        }

    def list_quotes(self, lot_id: int) -> list[dict[str, Any]]:
        self.get_lot(lot_id)
        rows = self.db.all(
            """
            SELECT quotes.*, suppliers.name AS supplier_name
            FROM quotes JOIN suppliers ON suppliers.id = quotes.supplier_id
            WHERE quotes.lot_id = ? ORDER BY quotes.id DESC
            """,
            (lot_id,),
        )
        for row in rows:
            row["vat_included"] = bool(row["vat_included"])
            row["items"] = self.db.all(
                """
                SELECT quote_items.*, lot_items.name AS lot_item_name, lot_items.unit
                FROM quote_items JOIN lot_items ON lot_items.id = quote_items.lot_item_id
                WHERE quote_items.quote_id = ? ORDER BY quote_items.id
                """,
                (row["id"],),
            )
        return rows

    def list_audit(self, limit: int = 50) -> list[dict[str, Any]]:
        if limit < 1 or limit > 200:
            raise ValueError("audit limit must be between 1 and 200")
        rows = self.db.all("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,))
        for row in rows:
            row["details"] = json.loads(row.pop("details_json"))
        return rows

    def comparison(self, lot_id: int) -> dict[str, Any]:
        lot = self.get_lot(lot_id)
        benchmark = self.lot_price_benchmark(lot_id)
        benchmark_by_item = {int(item["lot_item_id"]): item for item in benchmark["items"]}
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
            history_quote_total = Decimal("0")
            history_median_total = Decimal("0")
            history_matches = 0
            for item in items:
                item_id = int(item["lot_item_id"])
                item_benchmark = benchmark_by_item[item_id]
                median_price = item_benchmark["median_unit_price"]
                if median_price is None:
                    continue
                history_matches += 1
                quantity = requested[item_id]
                history_quote_total += quantity * Decimal(item["unit_price"])
                history_median_total += quantity * Decimal(str(median_price))
            variance = None
            potential_saving = None
            price_signal = "insufficient_history"
            if history_median_total > 0:
                variance = float(
                    (
                        (history_quote_total - history_median_total)
                        / history_median_total
                        * 100
                    ).quantize(Decimal("0.1"))
                )
                potential_saving = float(
                    max(Decimal("0"), history_quote_total - history_median_total)
                )
                if variance > 10:
                    price_signal = "above_history"
                elif variance < -10:
                    price_signal = "below_history"
                else:
                    price_signal = "near_history"
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
                    "history_coverage": f"{history_matches}/{len(requested)}",
                    "history_variance_pct": variance,
                    "potential_saving": potential_saving,
                    "price_signal": price_signal,
                }
            )
        return {
            "lot": lot,
            "price_benchmark": benchmark,
            "ranking_policy": "price_60_delivery_25_vat_15",
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
        allowed_types = {
            "paid_invoice",
            "tender_table",
            "project_section",
            "commercial_offer",
            "mail_attachment",
        }
        if document_type not in allowed_types:
            raise ValueError("unsupported document type")
        if not content or len(content) > 25 * 1024 * 1024:
            raise ValueError("document must be between 1 byte and 25 MB")
        suffix = Path(filename).suffix.lower()
        if suffix not in {".pdf", ".xlsx", ".csv", ".docx", ".png", ".jpg", ".jpeg"}:
            raise ValueError("unsupported mail attachment type")
        if suffix == ".pdf" and not content.startswith(b"%PDF-"):
            raise ValueError("invalid PDF payload")
        if suffix in {".xlsx", ".docx"} and not content.startswith(b"PK"):
            raise ValueError("invalid Office document payload")
        if suffix == ".png" and not content.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("invalid PNG payload")
        if suffix in {".jpg", ".jpeg"} and not content.startswith(b"\xff\xd8\xff"):
            raise ValueError("invalid JPEG payload")
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

    def analyze_fence_schedule(
        self, document_id: int, *, page_number: int
    ) -> dict[str, Any]:
        document = self.db.one("SELECT * FROM source_documents WHERE id = ?", (document_id,))
        if not document:
            raise NotFoundError("source document not found")
        if document["document_type"] != "project_section":
            raise ValueError("fence schedule extraction requires a project section document")
        if document["project_id"] is None:
            raise ValueError("project section document must be linked to a project")
        content = read_stored_pdf(document["storage_path"])
        text = extract_pdf_page(content, page_number)
        suggestion_data = extract_bulat_fence_schedule(
            text,
            page_number=page_number,
            source_document_id=document_id,
        )
        suggestion = self.register_procurement_suggestions(
            document_id, [suggestion_data]
        )[0]
        return {
            "suggestion": suggestion,
            "profile": "bulat_fence_schedule_v1",
            "source_page": page_number,
            "missing_rfq_fields": [
                "delivery_address_confirmation",
                "response_deadline",
                "coating",
                "color_ral",
                "mesh_cell",
                "rod_diameter",
                "delivery_or_pickup",
            ],
            "decision": "human_review_required",
        }

    def check_fence_reference(
        self, suggestion_id: int, reference_document_id: int
    ) -> dict[str, Any]:
        suggestion = self.get_procurement_suggestion(suggestion_id)
        reference_document = self.db.one(
            "SELECT * FROM source_documents WHERE id = ?", (reference_document_id,)
        )
        if not reference_document:
            raise NotFoundError("reference document not found")
        if reference_document["document_type"] not in {"paid_invoice", "commercial_offer"}:
            raise ValueError("reference check requires an invoice or commercial offer")
        content = read_stored_pdf(reference_document["storage_path"])
        text = extract_pdf_page(content, 1)
        reference = extract_bulat_invoice(text)
        result = compare_fence_documents(suggestion["items"], reference)
        with self.db.connection() as conn:
            conn.execute(
                """
                INSERT INTO document_reference_checks(
                    suggestion_id, reference_document_id, status, result_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(suggestion_id, reference_document_id) DO UPDATE SET
                    status=excluded.status,
                    result_json=excluded.result_json,
                    created_at=excluded.created_at
                """,
                (
                    suggestion_id,
                    reference_document_id,
                    result["status"],
                    json.dumps(result, ensure_ascii=False),
                    utcnow(),
                ),
            )
            self.db.audit(
                "reference_checked",
                "procurement_suggestion",
                suggestion_id,
                details={
                    "reference_document_id": reference_document_id,
                    "status": result["status"],
                    "can_use_as_current_quote": result["can_use_as_current_quote"],
                },
                conn=conn,
            )
        return {
            **result,
            "suggestion_id": suggestion_id,
            "reference_document_id": reference_document_id,
            "reference_filename": reference_document["filename"],
        }

    def list_reference_checks(self, suggestion_id: int) -> list[dict[str, Any]]:
        self.get_procurement_suggestion(suggestion_id)
        rows = self.db.all(
            """
            SELECT c.*, d.filename AS reference_filename
            FROM document_reference_checks c
            JOIN source_documents d ON d.id = c.reference_document_id
            WHERE c.suggestion_id = ?
            ORDER BY c.id DESC
            """,
            (suggestion_id,),
        )
        for row in rows:
            row["result"] = json.loads(row.pop("result_json"))
        return rows

    def list_source_documents(self, extraction_status: str = "") -> list[dict[str, Any]]:
        if extraction_status:
            return self.db.all(
                "SELECT * FROM source_documents WHERE extraction_status = ? ORDER BY id DESC",
                (extraction_status,),
            )
        return self.db.all("SELECT * FROM source_documents ORDER BY id DESC")

    @staticmethod
    def _decode_suggestion(row: dict[str, Any]) -> dict[str, Any]:
        row["items"] = json.loads(row.pop("items_json"))
        row["evidence"] = json.loads(row.pop("evidence_json"))
        return row

    def register_procurement_suggestions(
        self, document_id: int, suggestions: list[ProcurementSuggestionCreate]
    ) -> list[dict[str, Any]]:
        document = self.db.one("SELECT * FROM source_documents WHERE id = ?", (document_id,))
        if not document:
            raise NotFoundError("source document not found")
        if document["document_type"] != "project_section":
            raise ValueError("procurement suggestions require a project section document")
        if document["project_id"] is None:
            raise ValueError("project section document must be linked to a project")
        created: list[int] = []
        with self.db.connection() as conn:
            for suggestion in suggestions:
                try:
                    cursor = conn.execute(
                        """
                        INSERT INTO procurement_suggestions(
                            source_document_id, project_id, section_code, section_name,
                            lot_title, items_json, evidence_json, confidence, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            document_id,
                            document["project_id"],
                            suggestion.section_code,
                            suggestion.section_name,
                            suggestion.lot_title,
                            json.dumps(
                                [item.model_dump(mode="json") for item in suggestion.items],
                                ensure_ascii=False,
                            ),
                            json.dumps(suggestion.evidence, ensure_ascii=False),
                            suggestion.confidence,
                            utcnow(),
                        ),
                    )
                except Exception as exc:
                    if "UNIQUE constraint" in str(exc):
                        raise ConflictError("procurement suggestion already exists") from exc
                    raise
                created.append(int(cursor.lastrowid))
            conn.execute(
                "UPDATE source_documents SET extraction_status='needs_review' WHERE id=?",
                (document_id,),
            )
            self.db.audit(
                "extracted",
                "source_document",
                document_id,
                details={"procurement_suggestions": len(created), "decision": "human_review_required"},
                conn=conn,
            )
        return [self.get_procurement_suggestion(suggestion_id) for suggestion_id in created]

    def get_procurement_suggestion(self, suggestion_id: int) -> dict[str, Any]:
        row = self.db.one(
            """
            SELECT s.*, d.filename AS source_filename, p.name AS project_name,
                   p.region AS project_region, p.cluster AS project_cluster,
                   p.delivery_address
            FROM procurement_suggestions s
            JOIN source_documents d ON d.id = s.source_document_id
            JOIN projects p ON p.id = s.project_id
            WHERE s.id = ?
            """,
            (suggestion_id,),
        )
        if not row:
            raise NotFoundError("procurement suggestion not found")
        return self._decode_suggestion(row)

    @staticmethod
    def _finish_document_review(conn: Any, source_document_id: int) -> None:
        conn.execute(
            """
            UPDATE source_documents
            SET extraction_status='approved'
            WHERE id=? AND NOT EXISTS (
                SELECT 1 FROM procurement_suggestions
                WHERE source_document_id=? AND status IN ('needs_review', 'approving')
            )
            """,
            (source_document_id, source_document_id),
        )

    def list_procurement_suggestions(
        self, *, project_id: int | None = None, status: str = ""
    ) -> list[dict[str, Any]]:
        allowed_statuses = {"needs_review", "approved", "rejected"}
        if status and status not in allowed_statuses:
            raise ValueError("unsupported procurement suggestion status")
        clauses, params = [], []
        if project_id is not None:
            self.get_project(project_id)
            clauses.append("s.project_id = ?")
            params.append(project_id)
        if status:
            clauses.append("s.status = ?")
            params.append(status)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self.db.all(
            """
            SELECT s.*, d.filename AS source_filename, p.name AS project_name,
                   p.region AS project_region, p.cluster AS project_cluster,
                   p.delivery_address
            FROM procurement_suggestions s
            JOIN source_documents d ON d.id = s.source_document_id
            JOIN projects p ON p.id = s.project_id
            """
            + where
            + " ORDER BY s.id DESC",
            tuple(params),
        )
        return [self._decode_suggestion(row) for row in rows]

    def approve_procurement_suggestion(
        self, suggestion_id: int, data: ProcurementSuggestionApproval
    ) -> dict[str, Any]:
        suggestion = self.get_procurement_suggestion(suggestion_id)
        if suggestion["status"] != "needs_review":
            raise ConflictError("only suggestions awaiting review can be approved")
        with self.db.connection() as conn:
            claimed = conn.execute(
                "UPDATE procurement_suggestions SET status='approving' WHERE id=? AND status='needs_review'",
                (suggestion_id,),
            )
            if claimed.rowcount != 1:
                raise ConflictError("only suggestions awaiting review can be approved")
            section = conn.execute(
                "SELECT * FROM project_sections WHERE project_id=? AND code=?",
                (suggestion["project_id"], suggestion["section_code"]),
            ).fetchone()
            if section:
                section_id = int(section["id"])
            else:
                section_cursor = conn.execute(
                    """
                    INSERT INTO project_sections(project_id, code, name, description, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        suggestion["project_id"],
                        suggestion["section_code"],
                        suggestion["section_name"],
                        f"Создано из {suggestion['source_filename']}",
                        utcnow(),
                    ),
                )
                section_id = int(section_cursor.lastrowid)
                self.db.audit("created", "project_section", section_id, conn=conn)
            lot_cursor = conn.execute(
                """
                INSERT INTO lots(
                    project_id, section_id, title, region, cluster, delivery_address,
                    response_deadline, desired_delivery_date, currency,
                    rfq_requirements_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    suggestion["project_id"],
                    section_id,
                    suggestion["lot_title"],
                    suggestion["project_region"],
                    suggestion["project_cluster"],
                    (
                        data.rfq_requirements.delivery_address_confirmation
                        if data.rfq_requirements
                        and data.rfq_requirements.delivery_address_confirmation
                        else suggestion["delivery_address"]
                    ),
                    data.response_deadline.isoformat(),
                    data.desired_delivery_date.isoformat() if data.desired_delivery_date else None,
                    data.currency,
                    json.dumps(
                        data.rfq_requirements.model_dump(mode="json")
                        if data.rfq_requirements
                        else {},
                        ensure_ascii=False,
                    ),
                    utcnow(),
                ),
            )
            lot_id = int(lot_cursor.lastrowid)
            for item_data in suggestion["items"]:
                item = LotItemCreate.model_validate(item_data)
                conn.execute(
                    """
                    INSERT INTO lot_items(
                        lot_id, name, quantity, unit, specification,
                        source_document_id, source_page, source_reference
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        lot_id,
                        item.name,
                        str(item.quantity),
                        item.unit,
                        item.specification,
                        item.source_document_id,
                        item.source_page,
                        item.source_reference,
                    ),
                )
            self.db.audit(
                "created",
                "lot",
                lot_id,
                details={"project_name": suggestion["project_name"], "source": "ai_suggestion"},
                conn=conn,
            )
            conn.execute(
                """
                UPDATE procurement_suggestions
                SET status='approved', reviewed_by=?, reviewed_at=?, lot_id=?
                WHERE id=? AND status='approving'
                """,
                (data.approved_by, utcnow(), lot_id, suggestion_id),
            )
            self._finish_document_review(conn, suggestion["source_document_id"])
            self.db.audit(
                "approved",
                "procurement_suggestion",
                suggestion_id,
                actor=data.approved_by,
                details={"lot_id": lot_id, "source_document_id": suggestion["source_document_id"]},
                conn=conn,
            )
        return {
            "suggestion": self.get_procurement_suggestion(suggestion_id),
            "lot": self.get_lot(lot_id),
        }

    def reject_procurement_suggestion(
        self, suggestion_id: int, data: ProcurementSuggestionRejection
    ) -> dict[str, Any]:
        suggestion = self.get_procurement_suggestion(suggestion_id)
        if suggestion["status"] != "needs_review":
            raise ConflictError("only suggestions awaiting review can be rejected")
        with self.db.connection() as conn:
            rejected = conn.execute(
                """
                UPDATE procurement_suggestions
                SET status='rejected', reviewed_by=?, reviewed_at=?
                WHERE id=? AND status='needs_review'
                """,
                (data.reviewed_by, utcnow(), suggestion_id),
            )
            if rejected.rowcount != 1:
                raise ConflictError("only suggestions awaiting review can be rejected")
            self._finish_document_review(conn, suggestion["source_document_id"])
            self.db.audit(
                "rejected",
                "procurement_suggestion",
                suggestion_id,
                actor=data.reviewed_by,
                details={"reason": data.reason},
                conn=conn,
            )
        return self.get_procurement_suggestion(suggestion_id)

    @staticmethod
    def _mail_lot_id(subject: str) -> int | None:
        match = re.search(r"\[RFQ-(\d+)\]", subject, flags=re.IGNORECASE)
        return int(match.group(1)) if match else None

    @staticmethod
    def _mail_json(row: dict[str, Any]) -> dict[str, Any]:
        row["recipients"] = json.loads(row.pop("recipients_json", "[]") or "[]")
        row["cc"] = json.loads(row.pop("cc_json", "[]") or "[]")
        row["references"] = json.loads(row.pop("references_json", "[]") or "[]")
        return row

    def get_mail_message(self, message_id: int) -> dict[str, Any]:
        row = self.db.one(
            """
            SELECT mail_messages.*, suppliers.name AS supplier_name,
                   projects.name AS project_name, lots.title AS lot_title
            FROM mail_messages
            LEFT JOIN suppliers ON suppliers.id = mail_messages.supplier_id
            LEFT JOIN projects ON projects.id = mail_messages.project_id
            LEFT JOIN lots ON lots.id = mail_messages.lot_id
            WHERE mail_messages.id = ?
            """,
            (message_id,),
        )
        if not row:
            raise NotFoundError("mail message not found")
        row = self._mail_json(row)
        row["attachments"] = self.db.all(
            """
            SELECT mail_attachments.*, source_documents.extraction_status
            FROM mail_attachments
            LEFT JOIN source_documents
              ON source_documents.id = mail_attachments.source_document_id
            WHERE mail_attachments.mail_message_id = ?
            ORDER BY mail_attachments.id
            """,
            (message_id,),
        )
        return row

    def list_mail_messages(
        self,
        *,
        direction: str = "",
        status: str = "",
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        if direction and direction not in {"inbound", "outbound"}:
            raise ValueError("unsupported mail direction")
        allowed_statuses = {"received", "draft", "approved", "sent", "failed"}
        if status and status not in allowed_statuses:
            raise ValueError("unsupported mail status")
        if limit < 1 or limit > 500:
            raise ValueError("mail limit must be between 1 and 500")
        clauses: list[str] = []
        params: list[Any] = []
        if direction:
            clauses.append("mail_messages.direction = ?")
            params.append(direction)
        if status:
            clauses.append("mail_messages.status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        rows = self.db.all(
            f"""
            SELECT mail_messages.*, suppliers.name AS supplier_name,
                   projects.name AS project_name, lots.title AS lot_title,
                   COUNT(mail_attachments.id) AS attachment_count
            FROM mail_messages
            LEFT JOIN suppliers ON suppliers.id = mail_messages.supplier_id
            LEFT JOIN projects ON projects.id = mail_messages.project_id
            LEFT JOIN lots ON lots.id = mail_messages.lot_id
            LEFT JOIN mail_attachments
              ON mail_attachments.mail_message_id = mail_messages.id
            {where}
            GROUP BY mail_messages.id
            ORDER BY COALESCE(mail_messages.received_at, mail_messages.sent_at,
                              mail_messages.created_at) DESC,
                     mail_messages.id DESC
            LIMIT ?
            """,
            tuple(params),
        )
        return [self._mail_json(row) for row in rows]

    def mailbox_sync_state(self, mailbox_address: str) -> dict[str, Any]:
        return self.db.one(
            "SELECT * FROM mailbox_sync_state WHERE mailbox_address = ?",
            (mailbox_address.lower(),),
        ) or {
            "mailbox_address": mailbox_address.lower(),
            "uidvalidity": "",
            "last_uid": 0,
            "last_synced_at": None,
            "last_error": "",
        }

    def record_mail_sync_success(
        self,
        mailbox_address: str,
        *,
        uidvalidity: str,
        last_uid: int,
    ) -> None:
        now = utcnow()
        with self.db.connection() as conn:
            conn.execute(
                """
                INSERT INTO mailbox_sync_state(
                    mailbox_address, uidvalidity, last_uid, last_synced_at,
                    last_error, updated_at
                ) VALUES (?, ?, ?, ?, '', ?)
                ON CONFLICT(mailbox_address) DO UPDATE SET
                    uidvalidity=excluded.uidvalidity,
                    last_uid=excluded.last_uid,
                    last_synced_at=excluded.last_synced_at,
                    last_error='',
                    updated_at=excluded.updated_at
                """,
                (mailbox_address.lower(), uidvalidity, last_uid, now, now),
            )

    def record_mail_sync_failure(self, mailbox_address: str, error: str) -> None:
        now = utcnow()
        with self.db.connection() as conn:
            conn.execute(
                """
                INSERT INTO mailbox_sync_state(
                    mailbox_address, uidvalidity, last_uid, last_synced_at,
                    last_error, updated_at
                ) VALUES (?, '', 0, NULL, ?, ?)
                ON CONFLICT(mailbox_address) DO UPDATE SET
                    last_error=excluded.last_error,
                    updated_at=excluded.updated_at
                """,
                (mailbox_address.lower(), error[:500], now),
            )

    def ingest_inbound_mail(
        self,
        data: InboundMail,
        *,
        mailbox_address: str,
        imap_uid: int,
        imap_uidvalidity: str,
    ) -> dict[str, Any]:
        existing = self.db.one(
            "SELECT id FROM mail_messages WHERE message_id = ?", (data.message_id,)
        )
        supplier = self.db.one(
            "SELECT id FROM suppliers WHERE lower(email) = ? AND active = 1",
            (data.sender.lower(),),
        )
        supplier_id = int(supplier["id"]) if supplier else None
        lot_id = self._mail_lot_id(data.subject)
        project_id = None
        if lot_id is not None:
            lot = self.db.one("SELECT id, project_id FROM lots WHERE id = ?", (lot_id,))
            if lot:
                project_id = int(lot["project_id"])
            else:
                lot_id = None
        reply_row = None
        if data.in_reply_to:
            reply_row = self.db.one(
                "SELECT id, lot_id, project_id, supplier_id, thread_key FROM mail_messages WHERE message_id = ?",
                (data.in_reply_to,),
            )
        if reply_row:
            lot_id = lot_id or reply_row["lot_id"]
            project_id = project_id or reply_row["project_id"]
            supplier_id = supplier_id or reply_row["supplier_id"]
        if existing:
            stored_id = int(existing["id"])
        else:
            with self.db.connection() as conn:
                cursor = conn.execute(
                    """
                    INSERT INTO mail_messages(
                        message_id, mailbox_address, direction, sender,
                        recipients_json, cc_json, subject, body_text, status,
                        thread_key, in_reply_to, references_json, imap_uid,
                        imap_uidvalidity, supplier_id, project_id, lot_id,
                        reply_to_message_id, received_at, created_at
                    ) VALUES (?, ?, 'inbound', ?, ?, ?, ?, ?, 'received', ?, ?, ?,
                              ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        data.message_id,
                        mailbox_address.lower(),
                        data.sender,
                        json.dumps(data.recipients, ensure_ascii=False),
                        json.dumps(data.cc, ensure_ascii=False),
                        data.subject,
                        data.body_text,
                        str(reply_row["thread_key"]) if reply_row else data.thread_key,
                        data.in_reply_to,
                        json.dumps(data.references, ensure_ascii=False),
                        imap_uid,
                        imap_uidvalidity,
                        supplier_id,
                        project_id,
                        lot_id,
                        int(reply_row["id"]) if reply_row else None,
                        data.sent_at or utcnow(),
                        utcnow(),
                    ),
                )
                stored_id = int(cursor.lastrowid)
                self.db.audit(
                    "received",
                    "mail_message",
                    stored_id,
                    details={
                        "sender": data.sender,
                        "lot_id": lot_id,
                        "supplier_id": supplier_id,
                    },
                    conn=conn,
                )

        for attachment in data.attachments:
            digest = hashlib.sha256(attachment.content).hexdigest()
            source_document_id = None
            blocked_reason = ""
            try:
                document = self.register_source_document(
                    filename=attachment.filename,
                    content=attachment.content,
                    document_type="mail_attachment",
                    content_type=attachment.content_type,
                    project_id=project_id,
                    supplier_id=supplier_id,
                )
                source_document_id = int(document["id"])
            except (ValueError, RuntimeError) as exc:
                blocked_reason = str(exc)[:500]
            with self.db.connection() as conn:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO mail_attachments(
                        mail_message_id, source_document_id, filename, content_type,
                        size_bytes, sha256, blocked_reason, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        stored_id,
                        source_document_id,
                        Path(attachment.filename).name,
                        attachment.content_type,
                        len(attachment.content),
                        digest,
                        blocked_reason,
                        utcnow(),
                    ),
                )
        return self.get_mail_message(stored_id)

    def link_mail_message(
        self, message_id: int, data: MailLinkUpdate, *, actor: str
    ) -> dict[str, Any]:
        self.get_mail_message(message_id)
        project_id = data.project_id
        lot_id = data.lot_id
        supplier_id = data.supplier_id
        if lot_id is not None:
            lot = self.get_lot(lot_id)
            lot_project_id = int(lot["project_id"])
            if project_id is not None and project_id != lot_project_id:
                raise ValueError("mail project does not match the selected lot")
            project_id = lot_project_id
        elif project_id is not None:
            self.get_project(project_id)
        if supplier_id is not None:
            self.get_supplier(supplier_id)
        with self.db.connection() as conn:
            conn.execute(
                """
                UPDATE mail_messages
                SET supplier_id=?, project_id=?, lot_id=?
                WHERE id=?
                """,
                (supplier_id, project_id, lot_id, message_id),
            )
            conn.execute(
                """
                UPDATE source_documents
                SET supplier_id=COALESCE(supplier_id, ?),
                    project_id=COALESCE(project_id, ?)
                WHERE id IN (
                    SELECT source_document_id FROM mail_attachments
                    WHERE mail_message_id=? AND source_document_id IS NOT NULL
                )
                """,
                (supplier_id, project_id, message_id),
            )
            self.db.audit(
                "linked",
                "mail_message",
                message_id,
                actor=actor,
                details={
                    "supplier_id": supplier_id,
                    "project_id": project_id,
                    "lot_id": lot_id,
                },
                conn=conn,
            )
        return self.get_mail_message(message_id)

    def create_mail_draft(
        self,
        data: MailDraftCreate,
        *,
        mailbox_address: str,
        actor: str,
    ) -> dict[str, Any]:
        if "\r" in data.subject or "\n" in data.subject:
            raise ValueError("mail subject contains a line break")
        project_id = data.project_id
        lot_id = data.lot_id
        supplier_id = data.supplier_id
        if lot_id is not None:
            lot = self.get_lot(lot_id)
            if project_id is not None and project_id != int(lot["project_id"]):
                raise ValueError("mail project does not match the selected lot")
            project_id = int(lot["project_id"])
        elif project_id is not None:
            self.get_project(project_id)
        if supplier_id is not None:
            self.get_supplier(supplier_id)

        reply = None
        references: list[str] = []
        in_reply_to = ""
        thread_key = ""
        if data.reply_to_message_id is not None:
            reply = self.get_mail_message(data.reply_to_message_id)
            in_reply_to = str(reply["message_id"])
            references = [*reply["references"], in_reply_to]
            references = list(dict.fromkeys(value for value in references if value))
            thread_key = str(reply["thread_key"])
            supplier_id = supplier_id or reply["supplier_id"]
            project_id = project_id or reply["project_id"]
            lot_id = lot_id or reply["lot_id"]

        documents: list[dict[str, Any]] = []
        for document_id in dict.fromkeys(data.source_document_ids):
            document = self.db.one(
                "SELECT * FROM source_documents WHERE id = ?", (document_id,)
            )
            if not document:
                raise NotFoundError("source document not found")
            if not Path(document["storage_path"]).is_file():
                raise ConflictError("source document file is unavailable")
            documents.append(document)
        if sum(int(document["size_bytes"]) for document in documents) > 50 * 1024 * 1024:
            raise ValueError("mail attachments exceed the 50 MB limit")

        subject = data.subject.strip()
        if lot_id is not None and not re.search(
            rf"\[RFQ-{lot_id}\]", subject, flags=re.IGNORECASE
        ):
            subject = f"[RFQ-{lot_id}] {subject}"
        domain = mailbox_address.rsplit("@", 1)[-1] if "@" in mailbox_address else None
        external_message_id = make_msgid(domain=domain)
        if not thread_key:
            thread_key = external_message_id
        with self.db.connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO mail_messages(
                    message_id, mailbox_address, direction, sender,
                    recipients_json, cc_json, subject, body_text, status,
                    thread_key, in_reply_to, references_json, supplier_id,
                    project_id, lot_id, reply_to_message_id, created_at
                ) VALUES (?, ?, 'outbound', ?, ?, '[]', ?, ?, 'draft', ?, ?, ?,
                          ?, ?, ?, ?, ?)
                """,
                (
                    external_message_id,
                    mailbox_address.lower(),
                    mailbox_address.lower(),
                    json.dumps([data.recipient], ensure_ascii=False),
                    subject,
                    data.body,
                    thread_key,
                    in_reply_to,
                    json.dumps(references, ensure_ascii=False),
                    supplier_id,
                    project_id,
                    lot_id,
                    data.reply_to_message_id,
                    utcnow(),
                ),
            )
            stored_id = int(cursor.lastrowid)
            for document in documents:
                conn.execute(
                    """
                    INSERT INTO mail_attachments(
                        mail_message_id, source_document_id, filename, content_type,
                        size_bytes, sha256, blocked_reason, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, '', ?)
                    """,
                    (
                        stored_id,
                        document["id"],
                        document["filename"],
                        document["content_type"],
                        document["size_bytes"],
                        document["sha256"],
                        utcnow(),
                    ),
                )
            self.db.audit(
                "created",
                "mail_draft",
                stored_id,
                actor=actor,
                details={"recipient": data.recipient, "lot_id": lot_id},
                conn=conn,
            )
        return self.get_mail_message(stored_id)

    def create_mail_from_outbox(
        self, message_id: int, *, mailbox_address: str, actor: str
    ) -> dict[str, Any]:
        existing = self.db.one(
            "SELECT id FROM mail_messages WHERE outbox_message_id = ?", (message_id,)
        )
        if existing:
            return self.get_mail_message(int(existing["id"]))
        outbox = self.db.one(
            """
            SELECT outbox_messages.*, campaigns.lot_id
            FROM outbox_messages
            JOIN campaigns ON campaigns.id = outbox_messages.campaign_id
            WHERE outbox_messages.id = ?
            """,
            (message_id,),
        )
        if not outbox:
            raise NotFoundError("outbox message not found")
        if outbox["channel"] != "email":
            raise ConflictError("only email outbox messages can be delivered by SMTP")
        if outbox["status"] != "approved":
            raise ConflictError("outbox message must be approved first")
        draft = self.create_mail_draft(
            MailDraftCreate(
                recipient=outbox["recipient"],
                subject=outbox["subject"],
                body=outbox["body"],
                supplier_id=outbox["supplier_id"],
                lot_id=outbox["lot_id"],
            ),
            mailbox_address=mailbox_address,
            actor=actor,
        )
        with self.db.connection() as conn:
            conn.execute(
                "UPDATE mail_messages SET outbox_message_id=? WHERE id=?",
                (message_id, draft["id"]),
            )
        return self.approve_mail_draft(
            draft["id"],
            approved_by=str(outbox["approved_by"] or actor),
            comment="approved procurement outbox message",
        )

    def approve_mail_draft(
        self, message_id: int, *, approved_by: str, comment: str = ""
    ) -> dict[str, Any]:
        message = self.get_mail_message(message_id)
        if message["direction"] != "outbound" or message["status"] != "draft":
            raise ConflictError("only outbound draft messages can be approved")
        with self.db.connection() as conn:
            conn.execute(
                """
                UPDATE mail_messages
                SET status='approved', approved_by=?, approved_at=?, last_error=''
                WHERE id=? AND status='draft'
                """,
                (approved_by, utcnow(), message_id),
            )
            self.db.audit(
                "approved",
                "mail_message",
                message_id,
                actor=approved_by,
                details={"comment": comment},
                conn=conn,
            )
        return self.get_mail_message(message_id)

    def mail_delivery_payload(self, message_id: int) -> dict[str, Any]:
        message = self.get_mail_message(message_id)
        if message["direction"] != "outbound" or message["status"] != "approved":
            raise ConflictError("only approved outbound messages can be sent")
        if len(message["recipients"]) != 1 or message["cc"]:
            raise ConflictError(
                "mail delivery supports exactly one To recipient and no CC"
            )
        attachments = []
        for attachment in message["attachments"]:
            if attachment["blocked_reason"]:
                raise ConflictError("blocked attachment cannot be sent")
            document = self.db.one(
                "SELECT storage_path FROM source_documents WHERE id = ?",
                (attachment["source_document_id"],),
            )
            path = Path(str(document["storage_path"])) if document else None
            if path is None or not path.is_file():
                raise ConflictError("mail attachment file is unavailable")
            attachments.append(
                {
                    "filename": attachment["filename"],
                    "content_type": attachment["content_type"],
                    "content": path.read_bytes(),
                }
            )
        return {**message, "delivery_attachments": attachments}

    def mark_mail_sent(self, message_id: int, *, actor: str) -> dict[str, Any]:
        with self.db.connection() as conn:
            updated = conn.execute(
                """
                UPDATE mail_messages
                SET status='sent', sent_at=?, last_error=''
                WHERE id=? AND status='approved'
                """,
                (utcnow(), message_id),
            )
            if updated.rowcount != 1:
                raise ConflictError("mail message is not approved")
            self.db.audit(
                "sent", "mail_message", message_id, actor=actor, details={}, conn=conn
            )
        return self.get_mail_message(message_id)

    def mark_mail_failed(self, message_id: int, error: str, *, actor: str) -> dict[str, Any]:
        with self.db.connection() as conn:
            updated = conn.execute(
                """
                UPDATE mail_messages
                SET status='failed', last_error=?
                WHERE id=? AND status='approved'
                """,
                (error[:500], message_id),
            )
            if updated.rowcount != 1:
                raise ConflictError("mail message is not approved")
            self.db.audit(
                "failed",
                "mail_message",
                message_id,
                actor=actor,
                details={"error": error[:200]},
                conn=conn,
            )
        return self.get_mail_message(message_id)

    def get_mail_attachment(self, attachment_id: int) -> dict[str, Any]:
        attachment = self.db.one(
            """
            SELECT mail_attachments.*, source_documents.storage_path
            FROM mail_attachments
            LEFT JOIN source_documents
              ON source_documents.id = mail_attachments.source_document_id
            WHERE mail_attachments.id = ?
            """,
            (attachment_id,),
        )
        if not attachment:
            raise NotFoundError("mail attachment not found")
        if attachment["blocked_reason"] or not attachment["storage_path"]:
            raise ConflictError("mail attachment is blocked")
        if not Path(attachment["storage_path"]).is_file():
            raise ConflictError("mail attachment file is unavailable")
        return attachment

    def dashboard(self) -> dict[str, int]:
        result = {
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
                    self.db.one(
                        """
                        SELECT
                          (SELECT COUNT(*) FROM outbox_messages WHERE status='draft') +
                          (SELECT COUNT(*) FROM mail_messages WHERE status='draft') AS n
                        """
                    )
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
            "historical_prices": int(
                (
                    self.db.one(
                        "SELECT COUNT(*) AS n FROM purchase_history WHERE review_status='approved'"
                    )
                    or {"n": 0}
                )["n"]
            ),
        }
        result["unlinked_mail"] = int(
            (
                self.db.one(
                    """
                    SELECT COUNT(*) AS n FROM mail_messages
                    WHERE direction='inbound' AND lot_id IS NULL
                    """
                )
                or {"n": 0}
            )["n"]
        )
        return result
