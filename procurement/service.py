from __future__ import annotations

import hashlib
import json
import re
from difflib import SequenceMatcher
from datetime import date
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
from .regions import infer_region, normalize_region
from .region_routing import infer_cluster, resolve_cluster
from .templates import render_template
from .identity import trusted_actor
from .sandbox import payload_sha256, message_fingerprint, sandbox_adapter


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

    @staticmethod
    def validate_new_lot_policy(currency, cluster, project_cluster):
        if cluster and project_cluster and cluster != project_cluster:
            raise ValueError('Регион закупки не соответствует региону объекта. Выберите верный объект или исправьте регион.')
        if currency != 'RUB':
            raise ValueError('Новые закупки ведутся только в рублях (RUB)')

    def create_lot(self, data: LotCreate) -> dict[str, Any]:
        project = self.get_project(data.project_id)
        cluster = resolve_cluster(data.region, data.cluster or infer_cluster(data.region) or project["cluster"])
        self.validate_new_lot_policy(data.currency, cluster, project['cluster'])
        if data.section_id is not None:
            section = self.db.one(
                "SELECT id FROM project_sections WHERE id = ? AND project_id = ?",
                (data.section_id, data.project_id),
            )
            if not section:
                raise NotFoundError("project section not found")
        with self.db.connection() as conn:
            from .launch_workflow import LaunchWorkflow
            LaunchWorkflow(self)._documents(conn, data.project_id, data.attachment_document_ids)
            LaunchWorkflow(self)._documents(conn, data.project_id,
                [item.source_document_id for item in data.items if item.source_document_id is not None])
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
                        source_document_id, source_page, source_reference, delivery_date
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        str(item.delivery_date) if item.delivery_date else None,
                    ),
                )
            conn.executemany('INSERT INTO lot_attachments VALUES (?,?)',
                             [(lot_id, d) for d in sorted(set(data.attachment_document_ids))])
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
        row['attachments'] = self.db.all('''SELECT d.id AS document_id,d.filename,d.sha256,d.size_bytes
            FROM lot_attachments a JOIN source_documents d ON d.id=a.document_id WHERE a.lot_id=?''', (lot_id,))
        self._lot_mail_status([row])
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
        self._lot_mail_status(rows)
        return rows

    def _lot_mail_status(self, lots):
        from .mail_evidence import lot_mail_projection
        by_lot = {lot['id']: [] for lot in lots}
        if not by_lot:
            return
        # Batched projection scoped to the requested lots, never per-message queries.
        ids = list(by_lot)
        for offset in range(0, len(ids), 200):
            batch = ids[offset:offset + 200]
            records = self.db.all('''SELECT c.lot_id, m.status AS message_status, m.recipient,
                r.status, r.recipients_json, r.accepted_recipients_json, r.accepted_at,
                r.rfc_message_id, r.smtp_code
            FROM outbox_messages m JOIN campaigns c ON c.id=m.campaign_id
            LEFT JOIN mail_receipts r ON r.message_id=m.id
            WHERE c.lot_id IN (''' + ','.join('?' for _ in batch) + ')', tuple(batch))
            for record in records:
                by_lot[record['lot_id']].append(record)
        for lot in lots:
            lot.update(lot_mail_projection(lot, by_lot[lot['id']]))

    @staticmethod
    def _confirmed_cluster(lot_cluster: str, project_cluster: str) -> str:
        if lot_cluster not in {"cluster_1", "cluster_2"} or project_cluster not in {"cluster_1", "cluster_2"}:
            raise ValueError("lot and project cluster must be confirmed before procurement operations")
        if lot_cluster != project_cluster:
            raise ValueError("lot cluster must match project cluster")
        return lot_cluster

    def _lot_cluster(self, lot: dict) -> str:
        project = self.get_project(lot["project_id"])
        return self._confirmed_cluster(lot["cluster"], project["cluster"])

    @staticmethod
    def _supplier_cluster(supplier: dict, cluster: str) -> None:
        if supplier["cluster"] != cluster:
            raise ValueError("supplier cluster must match lot cluster")
        if not supplier.get("active", True):
            raise ValueError("inactive supplier cannot participate in procurement")

    def match_suppliers(self, lot_id: int) -> list[dict[str, Any]]:
        lot = self.get_lot(lot_id)
        cluster = self._lot_cluster(lot)
        search_text = " ".join([lot["title"], *(item["name"] for item in lot["items"])]).casefold()
        from .price_memory import material_key, designation_key
        from .catalog import normalize
        today = date.today().isoformat()
        known = self.db.all('''SELECT supplier_id,item_name,specification,region,valid_until FROM supplier_catalog_prices
            UNION ALL SELECT q.supplier_id,i.name,i.specification,l.region,COALESCE(q.valid_until,'')
            FROM quote_items qi JOIN quotes q ON q.id=qi.quote_id
            JOIN lot_items i ON i.id=qi.lot_item_id JOIN lots l ON l.id=q.lot_id
            UNION ALL SELECT supplier_id,item_name,'',region,'' FROM purchase_history
            WHERE review_status='approved' AND supplier_id IS NOT NULL''')
        candidates = []
        for supplier in self.list_suppliers():
            if supplier["cluster"] != cluster:
                continue
            region_match = lot["region"].casefold() in supplier["region"].casefold() or supplier[
                "region"
            ].casefold() in lot["region"].casefold()
            category_hits = sum(
                1 for category in supplier["categories"] if category.casefold() in search_text
            )
            def covers(item):
                label=material_key(item['name'])
                designation=designation_key(item['name'],item.get('specification',''))
                category=not item.get('specification') and any(
                    normalize(c) and normalize(c) in normalize(item['name'])
                    for c in supplier['categories'])
                history=any(row['supplier_id']==supplier['id'] and
                    (not row['valid_until'] or row['valid_until']>=today) and
                    normalize(row['region'])==normalize(lot['region']) and
                    material_key(row['item_name'])==label and
                    designation_key(row['item_name'],row['specification'])==designation
                    for row in known)
                return category or history
            item_coverage=sum(bool(covers(item)) for item in lot['items'])
            score = category_hits * 40 + int(region_match) * 25 + int(supplier["verified"]) * 20 + supplier[
                "rating"
            ] * 3
            if score > 0:
                supplier["match_score"] = round(score, 2)
                supplier["match_reasons"] = {
                    "cluster": cluster,
                    "region": region_match,
                    "category_hits": category_hits,
                    "verified": supplier["verified"],
                }
                supplier['item_coverage']=item_coverage
                supplier['auto_select']=bool(supplier['email'] and supplier['active'] and supplier['verified']
                    and region_match and lot['items'] and item_coverage==len(lot['items']))
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
        from .procurement_flow import lot_snapshot, items_text as snapshot_items_text, bind_campaign, validate_rendered_items
        if data.preview_sha256:
            from .procurement_flow import ProcurementFlow
            if ProcurementFlow(self).preview(lot_id,data)['preview_sha256'] != data.preview_sha256:
                raise ConflictError('Предпросмотр изменился; проверьте новый текст и получателей')
        with self.db.connection() as snapshot_conn:
            snapshot = lot_snapshot(snapshot_conn,lot_id,data.item_ids)
        if data.snapshot_sha256 and payload_sha256(snapshot) != data.snapshot_sha256:
            raise ConflictError('Выбранный лот изменился; обновите предпросмотр')
        lot = self.get_lot(lot_id)
        lot['items'] = snapshot['items']
        project = self.get_project(lot["project_id"])
        cluster = self._confirmed_cluster(lot["cluster"], project["cluster"])
        template = self.db.one("SELECT * FROM templates WHERE code = ?", (data.template_code,))
        if not template:
            raise NotFoundError("template not found")
        suppliers = [self.get_supplier(supplier_id) for supplier_id in dict.fromkeys(data.supplier_ids)]
        for supplier in suppliers:
            self._supplier_cluster(supplier, cluster)
        items_text = "\n".join(
            f"- {item['name']}: {item['quantity']} {item['unit']}"
            + (f"; {item['specification']}" if item["specification"] else "")
            + (f"; срок {item['delivery_date']}" if item.get('delivery_date') else "")
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
        items_text_current = snapshot_items_text(snapshot)
        if items_text_current != items_text:
            raise ConflictError('Состав запроса не совпадает с immutable snapshot')
        for _,_,_,body in prepared:validate_rendered_items(snapshot,body)
        fingerprint = payload_sha256({"lot_id": lot_id, "project_id": project["id"], "cluster": cluster,
            "template_code": data.template_code, "template_version": template["version"], "channel": data.channel,
            "attachments": lot.get('attachments', []),
            "messages": sorted([(supplier["id"], recipient, subject, body)
                                for supplier, recipient, subject, body in prepared])})
        request_key = f"lot:{lot_id}:client:{data.idempotency_key}" if data.idempotency_key else "auto:" + fingerprint
        expected_messages = sorted((supplier["id"], recipient, subject, body) for supplier, recipient, subject, body in prepared)
        with self.db.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if payload_sha256(lot_snapshot(conn,lot_id,[i['id'] for i in snapshot['items']])) != payload_sha256(snapshot):
                raise ConflictError('Лот изменился во время формирования запроса')
            current = conn.execute("""SELECT l.cluster AS lot_cluster,p.cluster AS project_cluster
                FROM lots l JOIN projects p ON p.id=l.project_id WHERE l.id=?""", (lot_id,)).fetchone()
            if not current or self._confirmed_cluster(current["lot_cluster"], current["project_cluster"]) != cluster:
                raise ConflictError("campaign cluster context changed; retry after review")
            current_attachments = [dict(r) for r in conn.execute('''SELECT d.id AS document_id,d.filename,d.sha256,d.size_bytes
                FROM lot_attachments a JOIN source_documents d ON d.id=a.document_id WHERE a.lot_id=? ORDER BY d.id''',(lot_id,))]
            if sorted(lot.get('attachments',[]),key=lambda a:a['document_id']) != current_attachments:
                raise ConflictError('Вложения изменились; обновите черновик')
            for supplier in suppliers:
                current_supplier = conn.execute("SELECT * FROM suppliers WHERE id=?", (supplier["id"],)).fetchone()
                if not current_supplier:
                    raise NotFoundError("supplier not found")
                self._supplier_cluster(dict(current_supplier), cluster)
                if any(current_supplier[k] != supplier[k] for k in ('name','email','telegram','max_contact')):
                    raise ConflictError('Реквизиты поставщика изменились; обновите предпросмотр')
            current_template = conn.execute('SELECT * FROM templates WHERE code=?',(data.template_code,)).fetchone()
            if not current_template or any(current_template[k] != template[k] for k in ('subject','body','version')):
                raise ConflictError('Шаблон изменился; обновите предпросмотр')

            def reuse(campaign_id):
                existing_campaign = conn.execute("SELECT lot_id,template_code,channel FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
                stored_messages = sorted(tuple(row) for row in conn.execute(
                    "SELECT supplier_id,recipient,subject,body FROM outbox_messages WHERE campaign_id=?", (campaign_id,)))
                if (not existing_campaign or tuple(existing_campaign) != (lot_id, data.template_code, data.channel)
                        or stored_messages != expected_messages):
                    raise ConflictError("stored campaign content changed; fresh human review is required")
                if not conn.execute('SELECT 1 FROM rfq_snapshots WHERE campaign_id=?',(campaign_id,)).fetchone():
                    statuses=[r[0] for r in conn.execute('SELECT status FROM outbox_messages WHERE campaign_id=?',(campaign_id,))]
                    if not data.preview_sha256 or not data.snapshot_sha256 or any(s!='draft' for s in statuses):
                        raise ConflictError('Архивный запрос требует нового подтверждённого предпросмотра')
                    bind_campaign(conn,campaign_id,snapshot)
                    self.db.audit('legacy_draft_reviewed','campaign',campaign_id,conn=conn)
                return self.get_campaign(campaign_id)

            existing = conn.execute("SELECT * FROM campaign_requests WHERE request_key=?", (request_key,)).fetchone()
            if existing:
                if existing["payload_sha256"] != fingerprint:
                    raise ConflictError("idempotency key was already used for different campaign content")
                return reuse(existing["campaign_id"])
            same_content = conn.execute("SELECT campaign_id FROM campaign_requests WHERE payload_sha256=? LIMIT 1", (fingerprint,)).fetchone()
            if same_content:
                conn.execute("INSERT INTO campaign_requests(request_key,payload_sha256,campaign_id,created_at) VALUES (?,?,?,?)",
                             (request_key, fingerprint, same_content["campaign_id"], utcnow()))
                return reuse(same_content["campaign_id"])
            # Do not backfill approvals or silently duplicate pre-ledger campaigns.
            legacy = conn.execute("""SELECT c.id FROM campaigns c LEFT JOIN campaign_requests r ON r.campaign_id=c.id
                WHERE c.lot_id=? AND c.template_code=? AND c.channel=? AND r.campaign_id IS NULL""",
                (lot_id, data.template_code, data.channel)).fetchall()
            for prior in legacy:
                prior_messages = sorted(tuple(row) for row in conn.execute(
                    "SELECT supplier_id,recipient,subject,body FROM outbox_messages WHERE campaign_id=?", (prior["id"],)))
                if prior_messages == expected_messages:
                    raise ConflictError("matching legacy campaign already exists; human review is required, no duplicate was created")
            cursor = conn.execute(
                "INSERT INTO campaigns(lot_id, template_code, channel, created_at) VALUES (?, ?, ?, ?)",
                (lot_id, data.template_code, data.channel, utcnow()),
            )
            campaign_id = cursor.lastrowid
            conn.execute("INSERT INTO campaign_requests(request_key,payload_sha256,campaign_id,created_at) VALUES (?,?,?,?)",
                         (request_key, fingerprint, campaign_id, utcnow()))
            for supplier, recipient, subject, body in prepared:
                message_id = conn.execute(
                    """
                    INSERT INTO outbox_messages(
                        campaign_id, supplier_id, channel, recipient, subject, body, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (campaign_id, supplier["id"], data.channel, recipient, subject, body, utcnow()),
                ).lastrowid
                conn.executemany('INSERT INTO outbox_attachments VALUES (?,?,?,?,?)',
                    [(message_id,a['document_id'],a['filename'],a['sha256'],a['size_bytes']) for a in lot.get('attachments',[])])
            bind_campaign(conn,campaign_id,snapshot)
            self._set_lot_progress(conn,lot_id,'rfq_draft')
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
        for message in row['messages']:
            message['attachments'] = self.db.all('SELECT document_id,filename,sha256,size_bytes FROM outbox_attachments WHERE message_id=? ORDER BY document_id',(message['id'],))
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
            if status not in {"draft", "approved", "queued", "sending", "sent", "failed"}:
                raise ValueError("unsupported outbox status")
            clauses.append("outbox_messages.status = ?")
            params.append(status)
        if lot_id is not None:
            self.get_lot(lot_id)
            clauses.append("campaigns.lot_id = ?")
            params.append(lot_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.db.all(
            f"""
            SELECT outbox_messages.*, suppliers.name AS supplier_name,
                   campaigns.lot_id, lots.title AS lot_title,
                   sandbox_deliveries.receipt_json AS sandbox_receipt_json
            FROM outbox_messages
            JOIN suppliers ON suppliers.id = outbox_messages.supplier_id
            JOIN campaigns ON campaigns.id = outbox_messages.campaign_id
            JOIN lots ON lots.id = campaigns.lot_id
            LEFT JOIN sandbox_deliveries ON sandbox_deliveries.message_id = outbox_messages.id
            {where}
            ORDER BY outbox_messages.id DESC
            """,
            tuple(params),
        )
        for row in rows:
            receipt = row.pop("sandbox_receipt_json")
            row["sandbox_receipt"] = json.loads(receipt) if receipt else None
            row['attachments'] = self.db.all('SELECT document_id,filename,sha256,size_bytes FROM outbox_attachments WHERE message_id=? ORDER BY document_id',(row['id'],))
            from .mail_delivery import journal
            row['delivery'] = journal(self.db, row['id'])
        return rows

    def _outbox_record(self, conn, message_id: int) -> dict:
        row = conn.execute("""
            SELECT m.*, c.lot_id, l.cluster AS lot_cluster, p.cluster AS project_cluster,
                   s.cluster AS supplier_cluster, s.active AS supplier_active
            FROM outbox_messages m JOIN campaigns c ON c.id=m.campaign_id
            JOIN lots l ON l.id=c.lot_id JOIN projects p ON p.id=l.project_id
            JOIN suppliers s ON s.id=m.supplier_id WHERE m.id=?
        """, (message_id,)).fetchone()
        if not row:
            raise NotFoundError("outbox message not found")
        message = dict(row)
        message['attachments'] = [dict(r) for r in conn.execute('SELECT document_id,filename,sha256,size_bytes FROM outbox_attachments WHERE message_id=? ORDER BY document_id',(message_id,))]
        return message

    def _outbox_context(self, conn, message_id: int) -> dict:
        message = self._outbox_record(conn, message_id)
        cluster = self._confirmed_cluster(message["lot_cluster"], message["project_cluster"])
        self._supplier_cluster({"cluster": message["supplier_cluster"], "active": message["supplier_active"]}, cluster)
        return message

    def approve_message(self, message_id: int, approved_by: str, comment: str = "", *, admin_policy_approval: bool = False) -> dict[str, Any]:
        approved_by = trusted_actor(approved_by).strip()
        if not approved_by or approved_by == "system":
            raise ValueError("a human approval actor is required")
        with self.db.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            message = self._outbox_context(conn, message_id)
            fingerprint = message_fingerprint(message)
            existing = conn.execute("SELECT * FROM outbox_approvals WHERE message_id=?", (message_id,)).fetchone()
            if existing:
                if message["status"] != "approved" or existing["payload_sha256"] != fingerprint:
                    raise ConflictError("approved content changed; create a new draft for human review")
                if not admin_policy_approval:
                    return dict(conn.execute("SELECT * FROM outbox_messages WHERE id=?", (message_id,)).fetchone())
            if message["status"] not in ({'draft','approved'} if admin_policy_approval else {'draft'}):
                raise ConflictError("only draft messages with fresh human approval can be simulated")
            now = utcnow()
            conn.execute("UPDATE outbox_messages SET status='approved', approved_by=?, approved_at=? WHERE id=?",
                         (approved_by, now, message_id))
            conn.execute("INSERT OR REPLACE INTO outbox_approvals(message_id,payload_sha256,approved_by,approved_at) VALUES (?,?,?,?)",
                         (message_id, fingerprint, approved_by, now))
            if admin_policy_approval:
                from .procurement_flow import ProcurementFlow
                context=ProcurementFlow(self).approval_context(conn,message['lot_id'])
                conn.execute('INSERT OR REPLACE INTO procurement_admin_approvals VALUES(?,?,?,?,?)',
                    (message_id,fingerprint,context,approved_by,now))
            self.db.audit("approved", "outbox_message", message_id, actor=approved_by,
                          details={"comment": comment, "dispatch": "approval_only", "payload_sha256": fingerprint}, conn=conn)
        return self.db.one("SELECT * FROM outbox_messages WHERE id = ?", (message_id,)) or {}

    def simulate_outbox(self, message_id: int) -> dict:
        """Explicit local simulation only. Persistent idempotency survives worker recreation."""
        with self.db.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            message = self._outbox_context(conn, message_id)
            fingerprint = message_fingerprint(message)
            approval = conn.execute("SELECT * FROM outbox_approvals WHERE message_id=?", (message_id,)).fetchone()
            if (not approval or message["status"] != "approved" or approval["payload_sha256"] != fingerprint
                    or approval["approved_by"] != message["approved_by"] or approval["approved_at"] != message["approved_at"]):
                raise ConflictError("unchanged content and explicit human approval are required for sandbox simulation")
            existing = conn.execute("SELECT * FROM sandbox_deliveries WHERE message_id=?", (message_id,)).fetchone()
            if existing:
                if existing["payload_sha256"] != fingerprint:
                    raise ConflictError("sandbox receipt does not match approved content")
                return json.loads(existing["receipt_json"])
            receipt = sandbox_adapter(message["channel"]).simulate(message, fingerprint)
            conn.execute("""INSERT INTO sandbox_deliveries(message_id,channel,payload_sha256,receipt_json,simulated_by,simulated_at)
                            VALUES (?,?,?,?,?,?)""", (message_id, message["channel"], fingerprint,
                            json.dumps(receipt, sort_keys=True), trusted_actor(), utcnow()))
            self.db.audit("simulated", "outbox_message", message_id, details=receipt, conn=conn)
            return receipt

    def add_quote(self, lot_id: int, data: QuoteCreate) -> dict[str, Any]:
        lot = self.get_lot(lot_id)
        cluster = self._lot_cluster(lot)
        if data.currency != lot["currency"]:
            raise ValueError("quote currency must match lot currency; exchange conversion is not configured")
        self._supplier_cluster(self.get_supplier(data.supplier_id), cluster)
        lot_item_ids = {int(item["id"]) for item in lot["items"]}
        submitted_ids = {item.lot_item_id for item in data.items}
        if not submitted_ids.issubset(lot_item_ids):
            raise ValueError("quote contains an item from another lot")
        if data.price_date and data.valid_until and data.valid_until < data.price_date:
            raise ValueError("Срок действия КП раньше даты цены")
        with self.db.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute("""SELECT l.cluster AS lot_cluster,p.cluster AS project_cluster,l.currency
                FROM lots l JOIN projects p ON p.id=l.project_id WHERE l.id=?""", (lot_id,)).fetchone()
            if not current or self._confirmed_cluster(current["lot_cluster"], current["project_cluster"]) != cluster:
                raise ConflictError("quote cluster context changed; retry after review")
            if current["currency"] != data.currency:
                raise ValueError("quote currency must match lot currency")
            current_supplier = conn.execute("SELECT cluster,active FROM suppliers WHERE id=?", (data.supplier_id,)).fetchone()
            if not current_supplier:
                raise NotFoundError("supplier not found")
            self._supplier_cluster(dict(current_supplier), cluster)
            if data.source_document_id is not None:
                source = conn.execute('SELECT * FROM source_documents WHERE id=?', (data.source_document_id,)).fetchone()
                if not source or source['document_type'] not in {'commercial_offer','price_list'}:
                    raise ValueError('Нужен исходный прайс или КП с подтверждённым ID')
                if source['project_id'] not in (None,lot['project_id']) or source['supplier_id'] not in (None,data.supplier_id):
                    raise ValueError('Исходное КП принадлежит другому объекту или поставщику')
            cursor = conn.execute(
                """
                INSERT INTO quotes(
                    lot_id, supplier_id, currency, vat_included, delivery_cost,
                    lead_days, payment_terms, warranty, valid_until, source_filename, created_at,
                    source_document_id, price_date, delivery_basis
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    data.source_document_id,
                    data.price_date.isoformat() if data.price_date else None,
                    data.delivery_basis,
                ),
            )
            quote_id = cursor.lastrowid
            for item in data.items:
                conn.execute(
                    """
                    INSERT INTO quote_items(
                        quote_id, lot_item_id, unit_price, offered_quantity, compliant, note, minimum_batch
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        quote_id,
                        item.lot_item_id,
                        str(item.unit_price),
                        str(item.offered_quantity) if item.offered_quantity is not None else None,
                        int(item.compliant),
                        item.note,
                        str(item.minimum_batch) if item.minimum_batch is not None else '',
                    ),
                )
            self._set_lot_progress(conn,lot_id,'quotes_received')
            self.db.audit("received", "quote", quote_id, details={"lot_id": lot_id}, conn=conn)
        return self.db.one("SELECT * FROM quotes WHERE id = ?", (quote_id,)) or {}

    @staticmethod
    def _set_lot_progress(conn, lot_id: int, stage: str) -> None:
        """Automatic intake/send cannot undo a human award or finalized order."""
        if stage not in {'rfq_draft','rfq_sent','quotes_received','comparison'}:
            raise ValueError('unsupported automatic lot stage')
        conn.execute('''UPDATE lots SET status=CASE
            WHEN EXISTS(SELECT 1 FROM procurement_decisions WHERE lot_id=lots.id AND stage='ordered') THEN 'ordered'
            WHEN EXISTS(SELECT 1 FROM procurement_decisions WHERE lot_id=lots.id AND stage='awarded') THEN 'awarded'
            WHEN status IN ('awarded','ordered') THEN status ELSE ? END WHERE id=?''',(stage,lot_id))

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
        data = data.model_copy(update={"confirmed_by": trusted_actor(data.confirmed_by)})
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
            SELECT purchase_history.*, suppliers.name AS supplier_name,
                   suppliers.cluster AS supplier_cluster
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
            SELECT purchase_history.*, suppliers.name AS supplier_name,
                   suppliers.cluster AS supplier_cluster
            FROM purchase_history
            LEFT JOIN suppliers ON suppliers.id = purchase_history.supplier_id
            WHERE {' AND '.join(clauses)}
            ORDER BY purchase_history.purchased_on DESC, purchase_history.id DESC
            LIMIT ?
            """,
            tuple(params),
        )
        for row in rows:
            value = row["vat_included"]
            row["vat_included"] = bool(value) if value in (0, 1) else None
        return rows

    def lot_price_benchmark(self, lot_id: int, *, vat_included: bool | None = None) -> dict[str, Any]:
        lot = self.get_lot(lot_id)
        cluster = self._lot_cluster(lot)
        region = normalize_region(lot["region"])
        history = self.list_purchase_history(limit=500)
        items: list[dict[str, Any]] = []
        for item in lot["items"]:
            candidates = []
            for record in history:
                # Exact normalized region is intentionally conservative: aliases,
                # missing regions or cluster drift require explicit human review.
                if (not region or normalize_region(record["region"]) != region
                        or infer_cluster(record["region"]) != cluster
                        or record["supplier_cluster"] != cluster):
                    continue
                if record["currency"] != lot["currency"]:
                    continue
                if not isinstance(record["vat_included"], bool):
                    continue
                if vat_included is not None and record["vat_included"] != vat_included:
                    continue
                if self._normalized_item_name(record["unit"]) != self._normalized_item_name(
                    item["unit"]
                ):
                    continue
                score = self._item_match_score(item["name"], record["item_name"])
                if score >= 0.75:
                    candidates.append((score, record))
            bases = {record["vat_included"] for _, record in candidates}
            ambiguous_vat = vat_included is None and len(bases) > 1
            if ambiguous_vat:
                candidates = []
            prices = [Decimal(record["unit_price"]) for _, record in candidates]
            items.append(
                {
                    "lot_item_id": item["id"],
                    "item_name": item["name"],
                    "unit": item["unit"],
                    "currency": lot["currency"],
                    "vat_included": vat_included if vat_included is not None else (
                        next(iter(bases)) if len(bases) == 1 else None),
                    "basis_status": "ambiguous_vat" if ambiguous_vat else (
                        "comparable" if prices else "insufficient_comparable_history"),
                    "source_purchase_ids": [record["id"] for _, record in candidates],
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
            "cluster": cluster,
            "region": lot["region"],
            "matched_items": sum(1 for item in items if item["history_count"]),
            "total_items": len(items),
            "items": items,
            "policy": "approved_purchases_same_region_cluster_currency_vat_unit_match_gte_0_75",
            "exclusions": "Missing or ambiguous region, supplier cluster or VAT basis is excluded; no FX or financing normalization",
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
        cluster = self._lot_cluster(lot)
        benchmark = self.lot_price_benchmark(lot_id)
        benchmarks_by_vat = {}
        requested = {int(item["id"]): Decimal(item["quantity"]) for item in lot["items"]}
        quotes = self.db.all(
            """
            SELECT quotes.*, suppliers.name AS supplier_name, suppliers.rating AS supplier_rating,
                   suppliers.cluster AS supplier_cluster
            FROM quotes JOIN suppliers ON suppliers.id = quotes.supplier_id
            WHERE quotes.lot_id = ? ORDER BY quotes.id
            """,
            (lot_id,),
        )
        rows = []
        for quote in quotes:
            if quote["supplier_cluster"] != cluster:
                raise ValueError("stored quote supplier cluster must match lot cluster before comparison")
            if quote["currency"] != lot["currency"]:
                raise ValueError("stored quote currency differs from lot; review is required before ranking")
            if quote["vat_included"] not in (0, 1):
                raise ValueError("stored quote VAT basis is ambiguous; review is required")
            vat_basis = bool(quote["vat_included"])
            if vat_basis not in benchmarks_by_vat:
                benchmarks_by_vat[vat_basis] = self.lot_price_benchmark(lot_id, vat_included=vat_basis)
            benchmark_by_item = {int(item["lot_item_id"]): item
                for item in benchmarks_by_vat[vat_basis]["items"]}
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
        _price_import: bool = False,
    ) -> dict[str, Any]:
        allowed_types = {"paid_invoice", "tender_table", "project_section", "commercial_offer",
                         "invoice", "price_list", "unknown"}
        if document_type not in allowed_types:
            raise ValueError("unsupported document type")
        from .upload_io import MAX_FILE, TOO_LARGE, UploadTooLarge, chunks, payload_sha256
        if len(content)>MAX_FILE:raise UploadTooLarge(TOO_LARGE)
        if not len(content):raise ValueError('Файл пуст')
        suffix = Path(filename).suffix.lower()
        from .table_ingest import safe_upload
        if suffix not in {".pdf", ".xlsx", ".csv", ".docx", '.png', '.jpg', '.jpeg'}:
            raise ValueError("only PDF, DOCX, XLSX and CSV documents are supported")
        if suffix == ".pdf" and not content.startswith(b"%PDF-"):
            raise ValueError("invalid PDF payload")
        if suffix in {".xlsx", ".docx"} and not content.startswith(b"PK"):
            raise ValueError("invalid Office document payload")
        # Legacy batch callers supply source-relative names, never destinations.
        filename = filename.replace('\\', '/').rsplit('/', 1)[-1]
        safe_upload(content, filename, {'.pdf','.xlsx','.csv','.docx','.png','.jpg','.jpeg'})
        if suffix in {'.png','.jpg','.jpeg'}:
            from PIL import Image
            from .upload_io import open_payload
            with open_payload(content) as image_stream, Image.open(image_stream) as image:
                expected_format = 'PNG' if suffix == '.png' else 'JPEG'
                if image.format != expected_format or image.width*image.height>40_000_000:
                    raise ValueError('Недопустимое или слишком большое изображение')
                image.verify()
        if project_id is not None:
            self.get_project(project_id)
        if supplier_id is not None:
            self.get_supplier(supplier_id)

        digest = payload_sha256(content)
        if self.db.path == ":memory:":
            raise RuntimeError("document storage is unavailable for in-memory database")

        storage_dir = Path(self.db.path).resolve().parent / "uploads" / document_type
        storage_dir.mkdir(parents=True, exist_ok=True)
        storage_path = storage_dir / f"{digest}{suffix}"
        with self.db.connection() as conn:
            # Claim the SHA before writing bytes: concurrent registrations cannot
            # observe a half-written file or race the unique source row.
            conn.execute('BEGIN IMMEDIATE')
            existing = conn.execute('SELECT * FROM source_documents WHERE sha256=?', (digest,)).fetchone()
            if existing:
                # Price import reviews caller-provided bytes without reassigning
                # the source's project/supplier ownership.
                reuse_for_review = _price_import and project_id is None and supplier_id is None
                if not reuse_for_review and (existing['project_id'] != project_id or existing['supplier_id'] != supplier_id):
                    raise ConflictError('same file already belongs to a different project/supplier; no cross-project reuse')
                return dict(existing)
            try:
                with storage_path.open('xb') as output:
                    for part in chunks(content):output.write(part)
            except FileExistsError:
                from .upload_io import FilePayload
                if payload_sha256(FilePayload(storage_path)) != digest:
                    raise ConflictError('immutable upload conflict')
            cursor = conn.execute(
                """
                INSERT INTO source_documents(
                    project_id, supplier_id, document_type, filename, content_type,
                    size_bytes, sha256, storage_path, created_at, extraction_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    'extracted_needs_review' if _price_import else 'pending_ai_extraction',
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
        from .launch_workflow import LaunchWorkflow
        from .document_analysis import extract_pdf_page_review
        launch = LaunchWorkflow(self)
        content = launch.document_file(document)
        extracted = extract_pdf_page_review(content, page_number)
        if extracted['mode'] == 'ocr':
            return launch.pdf_review(document, page_number, extracted)
        text = extracted['text']
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
        data = data.model_copy(update={"approved_by": trusted_actor(data.approved_by)})
        suggestion = self.get_procurement_suggestion(suggestion_id)
        if suggestion["status"] != "needs_review":
            raise ConflictError("only suggestions awaiting review can be approved")
        self.validate_new_lot_policy(data.currency, suggestion['project_cluster'], suggestion['project_cluster'])
        with self.db.connection() as conn:
            claimed = conn.execute(
                "UPDATE procurement_suggestions SET status='approving' WHERE id=? AND status='needs_review'",
                (suggestion_id,),
            )
            if claimed.rowcount != 1:
                raise ConflictError("only suggestions awaiting review can be approved")
            from .launch_workflow import LaunchWorkflow
            LaunchWorkflow(self)._documents(conn,suggestion['project_id'],[suggestion['source_document_id']])
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
            conn.execute('INSERT INTO lot_attachments(lot_id,document_id) VALUES (?,?)',
                         (lot_id,suggestion['source_document_id']))
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
        data = data.model_copy(update={"reviewed_by": trusted_actor(data.reviewed_by)})
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
        """Bound extraction before writes; claim each source atomically."""
        from .upload_io import MAX_BATCH
        if not 1 <= len(files) <= 20 or sum(len(content) for _, content in files) > MAX_BATCH:
            raise ValueError('batch requires 1-20 files within a 100 MB aggregate limit')
        if any(Path(filename).suffix.lower() == ".xls" for filename, _ in files):
            raise ValueError("legacy .xls is not supported; convert to .xlsx and review before import")
        created_by = trusted_actor(created_by)
        from .imports import extract_document, detect_cluster, supplier_dedup_key, price_validity_state

        # Parse outside SQLite locks and cap the entire batch, not only each file.
        # A limit violation has no database/file side effects.
        prepared = []
        total_items = 0
        errors: list[str] = []
        sha256_map: dict[str, str] = {}
        for filename, content in files:
            try:
                result = extract_document(content, filename)
            except Exception as exc:
                errors.append(f"{filename}: extraction failed — {type(exc).__name__}")
                continue
            total_items += len(result.items)
            if total_items > 10_000:
                raise ValueError('batch exceeds the 10000 extracted item aggregate limit')
            sha256_map[filename] = result.sha256
            errors.extend(f"{filename}: {error}"[:2000] for error in result.errors[:100])
            # A wholly failed extraction has no reviewable supplier/price rows.
            if not result.items:
                if not result.errors:
                    errors.append(f'{filename}: no price items extracted; nothing imported')
                continue
            prepared.append((filename, content, result))

        with self.db.connection() as conn:
            cursor = conn.execute("""
                INSERT INTO import_batches(status, filenames_json, total_files,
                    processed_files, sha256_json, created_by, created_at)
                VALUES ('processing', ?, ?, 0, ?, ?, ?)
            """, (json.dumps([fn for fn, _ in files], ensure_ascii=False), len(files),
                  json.dumps(sha256_map, ensure_ascii=False), created_by, utcnow()))
            batch_id = cursor.lastrowid
            self.db.audit('created', 'import_batch', batch_id, actor=created_by,
                          details={'total_files': len(files)}, conn=conn)

        inserted_count = 0
        new_drafts = False
        for filename, content, result in prepared:
            try:
                source = self.register_source_document(
                    filename=filename, content=content, document_type=result.document_type,
                    content_type='application/pdf' if Path(filename).suffix.lower()=='.pdf'
                    else 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                    _price_import=True)
                doc_id = int(source['id'])

                # One bounded transaction per source (<=10000 rows across the
                # entire batch). The read/claim/draft/price inserts are atomic.
                with self.db.connection() as conn:
                    conn.execute('BEGIN IMMEDIATE')
                    if conn.execute('SELECT 1 FROM price_history_entries WHERE source_document_id=? LIMIT 1',
                                    (doc_id,)).fetchone():
                        continue
                    draft_id = None
                    approved_supplier_id = None
                    created_draft = False
                    if result.supplier_name or result.supplier_tax_id:
                        dedup_key = supplier_dedup_key(result.supplier_tax_id, result.supplier_name,
                                                      result.supplier_email, result.supplier_phone)
                        existing_draft = conn.execute(
                            'SELECT id,status,approved_supplier_id FROM supplier_drafts WHERE dedup_key=?',
                            (dedup_key,)).fetchone()
                        superseded_id = None
                        if existing_draft and existing_draft['status']=='rejected':
                            # Keep the rejected decision, evidence and old price
                            # links intact. Retire only its active identity key;
                            # corrected evidence gets a fresh reviewable record.
                            superseded_id = existing_draft['id']
                            retired_key = hashlib.sha256(
                                f'{dedup_key}:rejected:{superseded_id}'.encode()).hexdigest()
                            conn.execute("UPDATE supplier_drafts SET dedup_key=? WHERE id=? AND status='rejected'",
                                         (retired_key,superseded_id))
                            existing_draft = None
                        if existing_draft:
                            draft_id = existing_draft['id']
                            if existing_draft['status']=='approved':
                                approved_supplier_id = existing_draft['approved_supplier_id']
                                if approved_supplier_id is None:
                                    raise ValueError('approved supplier draft has no authoritative supplier link')
                        else:
                            region = result.supplier_region or infer_region(result.supplier_name, result.supplier_tax_id)
                            cluster, cluster_status = detect_cluster(region)
                            raw = {'name':result.supplier_name, 'tax_id':result.supplier_tax_id,
                                   'region':result.supplier_region, 'email':result.supplier_email,
                                   'phone':result.supplier_phone, 'contact_person':result.supplier_contact,
                                   'document_date':result.document_date}
                            cursor = conn.execute("""
                                INSERT INTO supplier_drafts(import_batch_id,source_document_id,
                                    name,tax_id,region,cluster,email,phone,contact_person,
                                    raw_json,dedup_key,status,cluster_status,created_at)
                                VALUES (?,?,?,?,?,?,?,?,?,?,?,'needs_review',?,?)
                            """, (batch_id,doc_id,result.supplier_name,result.supplier_tax_id,region,cluster,
                                  result.supplier_email,result.supplier_phone,result.supplier_contact,
                                  json.dumps(raw,ensure_ascii=False),dedup_key,cluster_status,utcnow()))
                            draft_id = cursor.lastrowid
                            created_draft = True
                            if superseded_id is not None:
                                self.db.audit('superseded','supplier_draft',superseded_id,actor=created_by,
                                              details={'new_draft_id':draft_id,'source_document_id':doc_id,
                                                       'old_decision_preserved':True},conn=conn)
                    now = utcnow()
                    validity = price_validity_state(result.valid_until)
                    rows = [(
                        batch_id,doc_id,draft_id,approved_supplier_id,item.item_name,item.brand,
                        item.normalized_name,item.quantity,item.unit,item.unit_price,item.total_price,
                        item.currency,int(item.vat_included),result.document_date,result.valid_until,validity,
                        item.source_page,item.source_sheet,item.source_row,item.source_cell,item.source_text,'draft',now
                    ) for item in result.items]
                    conn.executemany("""
                        INSERT INTO price_history_entries(import_batch_id,source_document_id,supplier_draft_id,supplier_id,
                            item_name,brand,normalized_name,quantity,unit,unit_price,total_price,currency,vat_included,
                            document_date,valid_until,validity_state,source_page,source_sheet,source_row,source_cell,
                            source_text,status,created_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """, rows)
                # Count only committed rows/claims, not rolled-back attempts.
                inserted_count += len(rows)
                new_drafts = new_drafts or created_draft
            except Exception as exc:
                errors.append(f"{filename}: import failed — {type(exc).__name__}")

        final_status = 'needs_review' if inserted_count or new_drafts else 'failed' if errors else 'done'
        with self.db.connection() as conn:
            conn.execute("""
                UPDATE import_batches SET status=?,processed_files=?,sha256_json=?,errors_json=? WHERE id=?
            """, (final_status,len(files),json.dumps(sha256_map,ensure_ascii=False),
                  json.dumps(errors,ensure_ascii=False),batch_id))
        return self.get_import_batch(batch_id)

    def get_import_batch(self, batch_id: int) -> dict[str, Any]:
        row = self.db.one("SELECT * FROM import_batches WHERE id = ?", (batch_id,))
        if not row:
            raise NotFoundError("import batch not found")
        row["filenames"] = json.loads(row.pop("filenames_json"))
        row["sha256"] = json.loads(row.pop("sha256_json"))
        row["errors"] = json.loads(row.pop("errors_json"))
        row["supplier_drafts"] = self.db.all(
            "SELECT * FROM supplier_drafts WHERE import_batch_id = ? ORDER BY id",
            (batch_id,),
        )
        row["price_history_entries"] = self.db.all(
            """SELECT e.*, d.filename AS source_filename, d.document_type AS source_document_type
               FROM price_history_entries e LEFT JOIN source_documents d ON d.id=e.source_document_id
               WHERE e.import_batch_id = ? ORDER BY e.id""",
            (batch_id,),
        )
        return row

    def list_import_batches(self) -> list[dict[str, Any]]:
        rows = self.db.all("""
            SELECT b.*,
              (SELECT COUNT(*) FROM supplier_drafts s WHERE s.import_batch_id=b.id) AS supplier_draft_count,
              (SELECT COUNT(*) FROM price_history_entries e WHERE e.import_batch_id=b.id) AS price_entry_count,
              (SELECT COUNT(*) FROM price_history_entries e WHERE e.import_batch_id=b.id AND e.status='draft') AS draft_entry_count,
              (SELECT COUNT(*) FROM price_history_entries e WHERE e.import_batch_id=b.id AND e.status='confirmed') AS confirmed_entry_count
            FROM import_batches b ORDER BY b.id DESC
        """)
        for row in rows:
            row["filenames"] = json.loads(row.pop("filenames_json"))
            row["sha256"] = json.loads(row.pop("sha256_json"))
            row["errors"] = json.loads(row.pop("errors_json"))
        return rows

    def confirm_batch_entries(
        self,
        batch_id: int,
        entry_ids: list[int],
        confirmed_by: str,
    ) -> dict[str, Any]:
        return self._review_batch_entries(batch_id, entry_ids, confirmed_by, 'confirmed')

    def reject_batch_entries(self, batch_id: int, entry_ids: list[int], rejected_by: str) -> dict[str, Any]:
        return self._review_batch_entries(batch_id, entry_ids, rejected_by, 'rejected')

    def _review_batch_entries(self, batch_id, entry_ids, confirmed_by, target):
        confirmed_by = trusted_actor(confirmed_by)
        self.get_import_batch(batch_id)
        if not entry_ids or len(entry_ids) > 500 or any(type(eid) is not int or eid <= 0 for eid in entry_ids):
            raise ValueError("select between 1 and 500 positive entry IDs for review")
        entry_ids = list(dict.fromkeys(entry_ids))
        now = utcnow()
        with self.db.connection() as conn:
            # Validate the entire selection under one lock before changing any row.
            conn.execute("BEGIN IMMEDIATE")
            selected = []
            for eid in entry_ids:
                row = conn.execute(
                    "SELECT id,status FROM price_history_entries WHERE id=? AND import_batch_id=?", (eid, batch_id)
                ).fetchone()
                if not row:
                    raise NotFoundError(f"price_history_entry {eid} not in batch {batch_id}")
                if row["status"] not in {"draft", target}:
                    raise ConflictError("only draft or already " + target + " entries may be reviewed")
                if row["status"] == "draft":
                    selected.append(eid)
            for eid in selected:
                conn.execute(
                    "UPDATE price_history_entries SET status=?, confirmed_by=?, confirmed_at=? WHERE id=? AND status='draft'",
                    (target, confirmed_by, now, eid),
                )
            confirmed = len(selected)
            if confirmed:
                self.db.audit("entries_" + target, "import_batch", batch_id, actor=confirmed_by,
                              details={"entry_ids": selected, target: confirmed, "paid_purchase": False}, conn=conn)
                remaining = conn.execute(
                    "SELECT COUNT(*) AS n FROM price_history_entries WHERE import_batch_id=? AND status='draft'", (batch_id,)
                ).fetchone()
                if remaining["n"] == 0:
                    conn.execute("UPDATE import_batches SET status='done', reviewed_at=? WHERE id=?", (now, batch_id))
                    self.db.audit("completed", "import_batch", batch_id, actor=confirmed_by,
                                  details={target: confirmed}, conn=conn)
        return {target: confirmed}

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
            draft['match_error'] = ''
            try:
                matched = self._match_existing_supplier(draft, suppliers)
            except ConflictError as exc:
                matched = None
                draft['match_error'] = str(exc)
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
        """Unique evidence only; never pick an arbitrary row on ambiguity."""
        def unique(matches):
            if len(matches)>1:
                raise ConflictError('ambiguous supplier match; resolve supplier identity before approval')
            return matches[0] if matches else None
        draft_tax_id = re.sub(r"\D", "", str(draft.get("tax_id", "")))
        if draft_tax_id:
            # An explicit INN must not fall back to another person's email/name.
            return unique([s for s in suppliers if re.sub(r'\D','',str(s.get('tax_id','')))==draft_tax_id])

        draft_email = str(draft.get("email", "")).strip().casefold()
        if draft_email:
            matches=[s for s in suppliers if str(s.get('email','')).strip().casefold()==draft_email]
            if matches:
                return unique(matches)

        normalise_name = lambda value: " ".join(
            re.findall(r"[0-9a-zа-я]+", str(value).casefold().replace("ё", "е"))
        )
        draft_name = normalise_name(draft.get("name", ""))
        if draft_name:
            return unique([s for s in suppliers if normalise_name(s.get('name',''))==draft_name])
        return None

    def confirm_supplier_draft(
        self,
        draft_id: int,
        data: Any,
    ) -> dict[str, Any]:
        confirmed_by = trusted_actor(data.confirmed_by)
        from .models import SupplierCreate
        # Lock before the status check and keep the entire approval atomic.
        # No nested service connections may commit a supplier before its claim.
        with self.db.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            draft_row = conn.execute("SELECT * FROM supplier_drafts WHERE id=?", (draft_id,)).fetchone()
            if not draft_row:
                raise NotFoundError("supplier draft not found")
            draft = dict(draft_row)
            if draft['status'] not in ('needs_review', 'pending'):
                raise ConflictError(f"draft status is {draft['status']!r}, expected needs_review")
            name = data.name or draft['name']
            email = data.email or draft['email']
            phone = data.phone or draft['phone']
            region = data.region or draft['region']
            if not region:
                raise ValueError('supplier region is required before confirmation')
            cluster = resolve_cluster(region, data.cluster or draft['cluster'])
            if not cluster:
                raise ValueError('supplier cluster is required before confirmation')
            supplier_data = SupplierCreate(name=name, tax_id=draft['tax_id'], region=region,
                                           email=email, phone=phone, cluster=cluster, categories=[])
            matched = self._match_existing_supplier(
                {'name': name, 'tax_id': draft['tax_id'], 'email': email},
                [dict(row) for row in conn.execute(
                    'SELECT id,name,tax_id,region,cluster,email FROM suppliers WHERE active=1').fetchall()])
            if matched and matched['cluster'] and matched['cluster'] != cluster:
                raise ConflictError('existing supplier belongs to another cluster')
            if matched:
                supplier_id = int(matched['id'])
                conn.execute(
                    """
                    UPDATE suppliers
                    SET region=CASE WHEN region='' THEN ? ELSE region END,
                        cluster=CASE WHEN cluster='' THEN ? ELSE cluster END
                    WHERE id=?
                    """,
                    (region, cluster, supplier_id),
                )
            else:
                supplier_id = conn.execute('''INSERT INTO suppliers(
                    name,tax_id,region,email,phone,cluster,categories_json,rating,verified,source,created_at)
                    VALUES (?,?,?,?,?,?,?,3,0,'import_batch',?)''',
                    (supplier_data.name,supplier_data.tax_id,region,email,phone,cluster,'[]',utcnow())).lastrowid
                self.db.audit('created','supplier',supplier_id,actor=confirmed_by,
                              details={'source':'import_batch'},conn=conn)
            now = utcnow()
            conn.execute(
                """
                UPDATE supplier_drafts
                SET status='approved', confirmed_by=?, confirmed_at=?,
                    review_notes=?, name=?, region=?, cluster=?,
                    cluster_status='confirmed', approved_supplier_id=?
                WHERE id=?
                """,
                (
                    confirmed_by,
                    now,
                    data.review_notes,
                    name,
                    region,
                    cluster,
                    supplier_id,
                    draft_id,
                ),
            )
            # Supplier identity review is NOT financial or paid-invoice review.
            # Only attach the supplier; a separate explicit entry review is required.
            conn.execute(
                "UPDATE price_history_entries SET supplier_id=? WHERE supplier_draft_id=? AND supplier_id IS NULL",
                (supplier_id, draft_id),
            )
            self.db.audit(
                "approved",
                "supplier_draft",
                draft_id,
                actor=confirmed_by,
                details={
                    "supplier_id": supplier_id,
                    "reused_existing_supplier": bool(matched),
                    "cluster": cluster,
                },
                conn=conn,
            )
        return self.get_supplier(supplier_id)

    def reject_supplier_draft(self, draft_id: int, data: Any) -> dict[str, Any]:
        rejected_by = trusted_actor(data.rejected_by)
        with self.db.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            draft = conn.execute('SELECT status FROM supplier_drafts WHERE id=?', (draft_id,)).fetchone()
            if not draft:
                raise NotFoundError('supplier draft not found')
            if draft['status'] not in ('needs_review','pending'):
                raise ConflictError('only pending supplier drafts may be rejected')
            conn.execute(
                "UPDATE supplier_drafts SET status='rejected', confirmed_by=?, review_notes=? WHERE id=? AND status IN ('needs_review','pending')",
                (rejected_by, data.review_notes, draft_id),
            )
            self.db.audit(
                "rejected", "supplier_draft", draft_id,
                actor=rejected_by, details={"notes": data.review_notes}, conn=conn,
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
            if status not in {"draft", "confirmed", "rejected"}:
                raise ValueError("unsupported imported price status")
            clauses.append("e.status = ?")
            params.append(status)
        if supplier_id is not None:
            clauses.append("e.supplier_id = ?")
            params.append(supplier_id)
        if batch_id is not None:
            clauses.append("e.import_batch_id = ?")
            params.append(batch_id)
        if search:
            clauses.append("(e.item_name LIKE ? OR e.normalized_name LIKE ?)")
            params.extend([f"%{search}%", f"%{search}%"])
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        return self.db.all(
            f"""SELECT e.*, d.filename AS source_filename, d.document_type AS source_document_type
                FROM price_history_entries e LEFT JOIN source_documents d ON d.id=e.source_document_id
                {where} ORDER BY e.created_at DESC, e.id DESC LIMIT ?""",
            tuple(params),
        )
