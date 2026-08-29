from __future__ import annotations

import hashlib
import json
import re
from difflib import SequenceMatcher
from decimal import Decimal
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
from .ranking import rank_quotes
from .regions import infer_cluster, infer_region, resolve_cluster
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
            "historical_prices": int(
                (
                    self.db.one(
                        "SELECT COUNT(*) AS n FROM purchase_history WHERE review_status='approved'"
                    )
                    or {"n": 0}
                )["n"]
            ),
        }

    # ── PR #8: batch import & price-history service ───────────────────────────

    def create_import_batch(
        self,
        files: list[tuple[str, bytes]],
        *,
        created_by: str = "system",
    ) -> dict[str, Any]:
        """Create a batch import job and process all files synchronously."""
        from .imports import (
            extract_document,
            detect_cluster,
            supplier_dedup_key,
            price_validity_state,
        )
        from datetime import date

        filenames = [fn for fn, _ in files]
        sha256_map: dict[str, str] = {}
        errors: list[str] = []
        all_items: list[dict[str, Any]] = []

        with self.db.connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO import_batches(
                    status, filenames_json, total_files, processed_files,
                    sha256_json, created_by, created_at
                ) VALUES (?, ?, ?, 0, ?, ?, ?)
                """,
                (
                    "processing",
                    json.dumps(filenames, ensure_ascii=False),
                    len(files),
                    json.dumps({}, ensure_ascii=False),
                    created_by,
                    utcnow(),
                ),
            )
            batch_id = cursor.lastrowid
            self.db.audit(
                "created",
                "import_batch",
                batch_id,
                actor=created_by,
                details={"total_files": len(files)},
                conn=conn,
            )

        processed = 0
        for filename, content in files:
            try:
                result = extract_document(content, filename)
                sha256_map[filename] = result.sha256

                # Register source document (dedup by sha256)
                existing_doc = self.db.one(
                    "SELECT id FROM source_documents WHERE sha256 = ?",
                    (result.sha256,),
                )
                if existing_doc:
                    doc_id: int = existing_doc["id"]
                else:
                    with self.db.connection() as conn:
                        c = conn.execute(
                            """
                            INSERT INTO source_documents(
                                filename, content_type, size_bytes, sha256,
                                document_type, storage_path, extraction_status, created_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                filename,
                                "application/pdf" if filename.endswith(".pdf") else
                                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                len(content),
                                result.sha256,
                                'internal',
                                result.document_type,
                                "extracted",
                                utcnow(),
                            ),
                        )
                        doc_id = c.lastrowid

                # Create supplier draft (dedup by dedup_key)
                draft_id: int | None = None
                if result.supplier_name or result.supplier_tax_id:
                    dedup_key = supplier_dedup_key(
                        result.supplier_tax_id,
                        result.supplier_name,
                        result.supplier_email,
                        result.supplier_phone,
                    )
                    supplier_region = result.supplier_region or infer_region(
                        result.supplier_name, result.supplier_tax_id
                    )
                    cluster, cluster_status = detect_cluster(supplier_region)
                    raw_data = {
                        "name": result.supplier_name,
                        "tax_id": result.supplier_tax_id,
                        "region": result.supplier_region,
                        "email": result.supplier_email,
                        "phone": result.supplier_phone,
                        "contact_person": result.supplier_contact,
                        "document_date": result.document_date,
                    }
                    existing_draft = self.db.one(
                        "SELECT id FROM supplier_drafts WHERE dedup_key = ?",
                        (dedup_key,),
                    )
                    if existing_draft:
                        draft_id = existing_draft["id"]
                    else:
                        with self.db.connection() as conn:
                            c = conn.execute(
                                """
                                INSERT INTO supplier_drafts(
                                    import_batch_id, source_document_id,
                                    name, tax_id, region, cluster,
                                    email, phone, contact_person,
                                    raw_json, dedup_key,
                                    status, cluster_status, created_at
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                                """,
                                (
                                    batch_id,
                                    doc_id,
                                    result.supplier_name,
                                    result.supplier_tax_id,
                                    supplier_region,
                                    cluster,
                                    result.supplier_email,
                                    result.supplier_phone,
                                    result.supplier_contact,
                                    json.dumps(raw_data, ensure_ascii=False),
                                    dedup_key,
                                    "needs_review",
                                    cluster_status,
                                    utcnow(),
                                ),
                            )
                            draft_id = c.lastrowid

                # Store price history entries — skip if already imported (SHA256 dedup)
                existing_entry_count = self.db.one(
                    "SELECT COUNT(*) AS n FROM price_history_entries"
                    " WHERE source_document_id = ?",
                    (doc_id,),
                )
                entries_already_exist = (
                    existing_entry_count and existing_entry_count["n"] > 0
                )
                if entries_already_exist:
                    # Re-import: reuse existing entries, no duplicates
                    existing_for_doc = self.db.all(
                        "SELECT item_name, unit_price FROM price_history_entries"
                        " WHERE source_document_id = ?",
                        (doc_id,),
                    )
                    all_items.extend(existing_for_doc)
                else:
                    v_state = price_validity_state(result.valid_until)
                    for item in result.items:
                        with self.db.connection() as conn:
                            conn.execute(
                                """
                            INSERT INTO price_history_entries(
                                import_batch_id, source_document_id, supplier_draft_id,
                                item_name, brand, normalized_name,
                                quantity, unit, unit_price, total_price,
                                currency, vat_included,
                                document_date, valid_until, validity_state,
                                source_page, source_sheet, source_row, source_cell, source_text,
                                status, created_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                                """,
                                (
                                    batch_id,
                                    doc_id,
                                    draft_id,
                                    item.item_name,
                                    item.brand,
                                    item.normalized_name,
                                    item.quantity,
                                    item.unit,
                                    item.unit_price,
                                    item.total_price,
                                    item.currency,
                                    int(item.vat_included),
                                    result.document_date,
                                    result.valid_until,
                                    v_state,
                                    item.source_page,
                                    item.source_sheet,
                                    item.source_row,
                                    item.source_cell,
                                    item.source_text,
                                    "draft",
                                    utcnow(),
                                ),
                            )
                        all_items.append({"item_name": item.item_name, "unit_price": item.unit_price})

                if result.errors:
                    errors.extend(f"{filename}: {e}" for e in result.errors)

            except Exception as exc:
                errors.append(f"{filename}: extraction failed — {exc}")

            processed += 1

        # Update batch status
        final_status = "needs_review" if (all_items or errors) else "done"
        with self.db.connection() as conn:
            conn.execute(
                """
                UPDATE import_batches
                SET status = ?, processed_files = ?, sha256_json = ?
                WHERE id = ?
                """,
                (
                    final_status,
                    processed,
                    json.dumps(sha256_map, ensure_ascii=False),
                    batch_id,
                ),
            )

        return self.get_import_batch(batch_id)

    def get_import_batch(self, batch_id: int) -> dict[str, Any]:
        row = self.db.one("SELECT * FROM import_batches WHERE id = ?", (batch_id,))
        if not row:
            raise NotFoundError("import batch not found")
        row["filenames"] = json.loads(row.pop("filenames_json"))
        row["sha256"] = json.loads(row.pop("sha256_json"))
        row["supplier_drafts"] = self.db.all(
            "SELECT * FROM supplier_drafts WHERE import_batch_id = ? ORDER BY id",
            (batch_id,),
        )
        row["price_history_entries"] = self.db.all(
            "SELECT * FROM price_history_entries WHERE import_batch_id = ? ORDER BY id",
            (batch_id,),
        )
        return row

    def list_import_batches(self) -> list[dict[str, Any]]:
        rows = self.db.all("SELECT * FROM import_batches ORDER BY id DESC")
        for row in rows:
            row["filenames"] = json.loads(row.pop("filenames_json"))
            row["sha256"] = json.loads(row.pop("sha256_json"))
        return rows

    def confirm_batch_entries(
        self,
        batch_id: int,
        entry_ids: list[int],
        confirmed_by: str,
    ) -> dict[str, Any]:
        self.get_import_batch(batch_id)
        now = utcnow()
        confirmed = 0
        for eid in entry_ids:
            row = self.db.one(
                "SELECT id FROM price_history_entries WHERE id = ? AND import_batch_id = ?",
                (eid, batch_id),
            )
            if not row:
                raise NotFoundError(f"price_history_entry {eid} not in batch {batch_id}")
            with self.db.connection() as conn:
                conn.execute(
                    "UPDATE price_history_entries SET status='confirmed', confirmed_by=?, confirmed_at=? WHERE id=?",
                    (confirmed_by, now, eid),
                )
            confirmed += 1
        # Mark batch done if all entries reviewed
        remaining = self.db.one(
            "SELECT COUNT(*) AS n FROM price_history_entries WHERE import_batch_id=? AND status='draft'",
            (batch_id,),
        )
        if remaining and remaining["n"] == 0:
            with self.db.connection() as conn:
                conn.execute(
                    "UPDATE import_batches SET status='done', reviewed_at=? WHERE id=?",
                    (now, batch_id),
                )
            self.db.audit("completed", "import_batch", batch_id, actor=confirmed_by,
                          details={"confirmed": confirmed})
        return {"confirmed": confirmed}

    def list_supplier_drafts(
        self, status: str = "", batch_id: int | None = None
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if batch_id is not None:
            clauses.append("import_batch_id = ?")
            params.append(batch_id)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        drafts = self.db.all(
            f"SELECT * FROM supplier_drafts {where} ORDER BY id DESC",
            tuple(params),
        )
        suppliers = self.db.all(
            "SELECT id, name, tax_id, region, cluster, email "
            "FROM suppliers WHERE active = 1"
        )
        for draft in drafts:
            matched = self._match_existing_supplier(draft, suppliers)
            draft["matched_supplier_id"] = matched["id"] if matched else None
            draft["matched_supplier_name"] = matched["name"] if matched else ""
            draft["suggested_region"] = draft["region"] or (
                matched["region"]
                if matched
                else infer_region(draft["name"], draft["tax_id"])
            )
            draft["suggested_cluster"] = (
                draft["cluster"]
                or (matched["cluster"] if matched else "")
                or infer_cluster(draft["suggested_region"])
            )
        return drafts

    @staticmethod
    def _match_existing_supplier(
        draft: dict[str, Any], suppliers: list[dict[str, Any]]
    ) -> dict[str, Any] | None:
        """Match deterministically: tax id, then email, then exact normalised name."""
        draft_tax_id = re.sub(r"\D", "", str(draft.get("tax_id", "")))
        if draft_tax_id:
            for supplier in suppliers:
                if re.sub(r"\D", "", str(supplier.get("tax_id", ""))) == draft_tax_id:
                    return supplier

        draft_email = str(draft.get("email", "")).strip().casefold()
        if draft_email:
            for supplier in suppliers:
                if str(supplier.get("email", "")).strip().casefold() == draft_email:
                    return supplier

        normalise_name = lambda value: " ".join(
            re.findall(r"[0-9a-zа-я]+", str(value).casefold().replace("ё", "е"))
        )
        draft_name = normalise_name(draft.get("name", ""))
        if draft_name:
            for supplier in suppliers:
                if normalise_name(supplier.get("name", "")) == draft_name:
                    return supplier
        return None

    def confirm_supplier_draft(
        self,
        draft_id: int,
        data: Any,
    ) -> dict[str, Any]:
        from .models import SupplierCreate
        draft = self.db.one("SELECT * FROM supplier_drafts WHERE id = ?", (draft_id,))
        if not draft:
            raise NotFoundError("supplier draft not found")
        if draft["status"] not in ("needs_review", "pending"):
            raise ValueError(f"draft status is {draft['status']!r}, expected needs_review")

        name = data.name or draft["name"]
        email = data.email or draft["email"]
        phone = data.phone or draft["phone"]
        contact = data.contact_person or draft["contact_person"]
        region = data.region or draft["region"]
        if not region:
            raise ValueError("supplier region is required before confirmation")
        cluster = resolve_cluster(region, data.cluster or draft["cluster"])
        if not cluster:
            raise ValueError("supplier cluster is required before confirmation")

        matched = self._match_existing_supplier(
            {
                "name": name,
                "tax_id": draft["tax_id"],
                "email": email,
            },
            self.db.all(
                "SELECT id, name, tax_id, region, cluster, email "
                "FROM suppliers WHERE active = 1"
            ),
        )
        if matched and matched["cluster"] and matched["cluster"] != cluster:
            raise ConflictError("existing supplier belongs to another cluster")

        if matched:
            supplier_id = int(matched["id"])
            with self.db.connection() as conn:
                conn.execute(
                    """
                    UPDATE suppliers
                    SET region=CASE WHEN region='' THEN ? ELSE region END,
                        cluster=CASE WHEN cluster='' THEN ? ELSE cluster END
                    WHERE id=?
                    """,
                    (region, cluster, supplier_id),
                )
            supplier = self.get_supplier(supplier_id)
        else:
            supplier_data = SupplierCreate(
                name=name,
                tax_id=draft["tax_id"],
                region=region,
                email=email,
                phone=phone,
                cluster=cluster,
                categories=[],
            )
            supplier = self.create_supplier(supplier_data, source="import_batch")

        now = utcnow()
        with self.db.connection() as conn:
            conn.execute(
                """
                UPDATE supplier_drafts
                SET status='approved', confirmed_by=?, confirmed_at=?,
                    review_notes=?, name=?, region=?, cluster=?,
                    cluster_status='confirmed'
                WHERE id=?
                """,
                (
                    data.confirmed_by,
                    now,
                    data.review_notes,
                    name,
                    region,
                    cluster,
                    draft_id,
                ),
            )
            # Attach confirmed supplier to price history entries
            conn.execute(
                "UPDATE price_history_entries SET supplier_id=?, status='confirmed' WHERE supplier_draft_id=? AND status='draft'",
                (supplier["id"], draft_id),
            )
            self.db.audit(
                "approved",
                "supplier_draft",
                draft_id,
                actor=data.confirmed_by,
                details={
                    "supplier_id": supplier["id"],
                    "reused_existing_supplier": bool(matched),
                    "cluster": cluster,
                },
                conn=conn,
            )
        return supplier

    def reject_supplier_draft(self, draft_id: int, data: Any) -> dict[str, Any]:
        draft = self.db.one("SELECT * FROM supplier_drafts WHERE id = ?", (draft_id,))
        if not draft:
            raise NotFoundError("supplier draft not found")
        with self.db.connection() as conn:
            conn.execute(
                "UPDATE supplier_drafts SET status='rejected', confirmed_by=?, review_notes=? WHERE id=?",
                (data.rejected_by, data.review_notes, draft_id),
            )
            self.db.audit(
                "rejected", "supplier_draft", draft_id,
                actor=data.rejected_by, details={"notes": data.review_notes}, conn=conn,
            )
        return self.db.one("SELECT * FROM supplier_drafts WHERE id = ?", (draft_id,)) or {}

    def list_price_history_entries(
        self,
        search: str = "",
        status: str = "confirmed",
        supplier_id: int | None = None,
        batch_id: int | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if supplier_id is not None:
            clauses.append("supplier_id = ?")
            params.append(supplier_id)
        if batch_id is not None:
            clauses.append("import_batch_id = ?")
            params.append(batch_id)
        if search:
            clauses.append("(item_name LIKE ? OR normalized_name LIKE ?)")
            params.extend([f"%{search}%", f"%{search}%"])
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        return self.db.all(
            f"SELECT * FROM price_history_entries {where} ORDER BY created_at DESC LIMIT ?",
            tuple(params),
        )
