from __future__ import annotations

import hmac
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, Header, HTTPException, Query, UploadFile
from fastapi.responses import HTMLResponse

from .config import Settings
from .db import Database
from .imports import parse_supplier_table
from .models import (
    ApprovalDecision,
    CampaignCreate,
    LotCreate,
    ProcurementSuggestionApproval,
    ProcurementSuggestionBatch,
    ProcurementSuggestionRejection,
    PurchaseHistoryCreate,
    ProjectCreate,
    QuoteCreate,
    SectionCreate,
    SupplierCreate,
    TemplateUpsert,
)
from .service import ConflictError, NotFoundError, ProcurementService


settings = Settings.from_env()
db = Database(settings.db_path)
service = ProcurementService(db)


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.initialize()
    yield


app = FastAPI(
    title="DAS Снабжение",
    version="0.4.0",
    description="Internal supplier RFQ and tender comparison workflow",
    lifespan=lifespan,
)


def require_api_key(x_api_key: str = Header(default="")) -> None:
    if settings.api_key and not hmac.compare_digest(x_api_key, settings.api_key):
        raise HTTPException(status_code=403, detail="invalid API key")


def handle_domain_error(exc: Exception) -> HTTPException:
    if isinstance(exc, NotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, ConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=422, detail=str(exc))


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "das-procurement-agent", "outbox": settings.outbox_mode}


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    path = Path(__file__).parent / "static" / "index.html"
    return HTMLResponse(path.read_text(encoding="utf-8"))


@app.get("/api/dashboard", dependencies=[Depends(require_api_key)])
def dashboard():
    return service.dashboard()


@app.post("/api/projects", dependencies=[Depends(require_api_key)], status_code=201)
def create_project(data: ProjectCreate):
    try:
        return service.create_project(data)
    except Exception as exc:
        raise handle_domain_error(exc) from exc


@app.get("/api/projects", dependencies=[Depends(require_api_key)])
def list_projects():
    return service.list_projects()


@app.get("/api/projects/{project_id}", dependencies=[Depends(require_api_key)])
def get_project(project_id: int):
    try:
        return service.get_project(project_id)
    except Exception as exc:
        raise handle_domain_error(exc) from exc


@app.post("/api/projects/{project_id}/sections", dependencies=[Depends(require_api_key)], status_code=201)
def add_section(project_id: int, data: SectionCreate):
    try:
        return service.add_section(project_id, data)
    except Exception as exc:
        raise handle_domain_error(exc) from exc


@app.post("/api/suppliers", dependencies=[Depends(require_api_key)], status_code=201)
def create_supplier(data: SupplierCreate):
    try:
        return service.create_supplier(data)
    except Exception as exc:
        raise handle_domain_error(exc) from exc


@app.get("/api/suppliers", dependencies=[Depends(require_api_key)])
def list_suppliers(region: str = Query(default=""), category: str = Query(default="")):
    return service.list_suppliers(region=region, category=category)


@app.post("/api/suppliers/import", dependencies=[Depends(require_api_key)])
async def import_suppliers(file: UploadFile = File(...), commit: bool = Query(default=False)):
    try:
        content = await file.read()
        preview = parse_supplier_table(content, file.filename or "")
        imported = []
        if commit:
            for supplier in preview.rows:
                try:
                    imported.append(service.create_supplier(supplier, source=f"import:{file.filename}"))
                except ConflictError as exc:
                    preview.errors.append({"row": None, "supplier": supplier.name, "error": str(exc)})
        return {
            "mode": "commit" if commit else "preview",
            "headers": preview.headers,
            "valid_rows": len(preview.rows),
            "imported": len(imported),
            "rows": [row.model_dump(mode="json") for row in preview.rows[:100]],
            "errors": preview.errors,
        }
    except Exception as exc:
        raise handle_domain_error(exc) from exc


@app.post("/api/documents", dependencies=[Depends(require_api_key)], status_code=201)
async def upload_source_document(
    file: UploadFile = File(...),
    document_type: str = Query(...),
    project_id: int | None = Query(default=None),
    supplier_id: int | None = Query(default=None),
):
    try:
        content = await file.read(25 * 1024 * 1024 + 1)
        result = service.register_source_document(
            filename=file.filename or "",
            content=content,
            document_type=document_type,
            content_type=file.content_type or "application/octet-stream",
            project_id=project_id,
            supplier_id=supplier_id,
        )
        return {**result, "storage_path": "internal", "next_step": "ai_extraction_then_human_review"}
    except Exception as exc:
        raise handle_domain_error(exc) from exc


@app.get("/api/documents", dependencies=[Depends(require_api_key)])
def list_source_documents(extraction_status: str = Query(default="")):
    rows = service.list_source_documents(extraction_status)
    return [{**row, "storage_path": "internal"} for row in rows]


@app.post(
    "/api/documents/{document_id}/procurement-suggestions",
    dependencies=[Depends(require_api_key)],
    status_code=201,
)
def register_procurement_suggestions(document_id: int, data: ProcurementSuggestionBatch):
    try:
        return service.register_procurement_suggestions(document_id, data.suggestions)
    except Exception as exc:
        raise handle_domain_error(exc) from exc


@app.get("/api/procurement-suggestions", dependencies=[Depends(require_api_key)])
def list_procurement_suggestions(
    project_id: int | None = Query(default=None), status: str = Query(default="")
):
    try:
        return service.list_procurement_suggestions(project_id=project_id, status=status)
    except Exception as exc:
        raise handle_domain_error(exc) from exc


@app.post(
    "/api/procurement-suggestions/{suggestion_id}/approve",
    dependencies=[Depends(require_api_key)],
)
def approve_procurement_suggestion(suggestion_id: int, data: ProcurementSuggestionApproval):
    try:
        return service.approve_procurement_suggestion(suggestion_id, data)
    except Exception as exc:
        raise handle_domain_error(exc) from exc


@app.post(
    "/api/procurement-suggestions/{suggestion_id}/reject",
    dependencies=[Depends(require_api_key)],
)
def reject_procurement_suggestion(suggestion_id: int, data: ProcurementSuggestionRejection):
    try:
        return service.reject_procurement_suggestion(suggestion_id, data)
    except Exception as exc:
        raise handle_domain_error(exc) from exc


@app.post("/api/lots", dependencies=[Depends(require_api_key)], status_code=201)
def create_lot(data: LotCreate):
    try:
        return service.create_lot(data)
    except Exception as exc:
        raise handle_domain_error(exc) from exc


@app.get("/api/lots", dependencies=[Depends(require_api_key)])
def list_lots():
    return service.list_lots()


@app.get("/api/lots/{lot_id}", dependencies=[Depends(require_api_key)])
def get_lot(lot_id: int):
    try:
        return service.get_lot(lot_id)
    except Exception as exc:
        raise handle_domain_error(exc) from exc


@app.get("/api/lots/{lot_id}/supplier-matches", dependencies=[Depends(require_api_key)])
def supplier_matches(lot_id: int):
    try:
        return service.match_suppliers(lot_id)
    except Exception as exc:
        raise handle_domain_error(exc) from exc


@app.post("/api/lots/{lot_id}/campaigns", dependencies=[Depends(require_api_key)], status_code=201)
def create_campaign(lot_id: int, data: CampaignCreate):
    try:
        return service.create_campaign(lot_id, data)
    except Exception as exc:
        raise handle_domain_error(exc) from exc


@app.get("/api/campaigns", dependencies=[Depends(require_api_key)])
def list_campaigns(lot_id: int | None = Query(default=None)):
    try:
        return service.list_campaigns(lot_id)
    except Exception as exc:
        raise handle_domain_error(exc) from exc


@app.get("/api/outbox", dependencies=[Depends(require_api_key)])
def list_outbox(
    status: str = Query(default=""),
    lot_id: int | None = Query(default=None),
):
    try:
        return service.list_outbox(status=status, lot_id=lot_id)
    except Exception as exc:
        raise handle_domain_error(exc) from exc


@app.post("/api/outbox/{message_id}/approve", dependencies=[Depends(require_api_key)])
def approve_message(message_id: int, decision: ApprovalDecision):
    try:
        return service.approve_message(message_id, decision.approved_by, decision.comment)
    except Exception as exc:
        raise handle_domain_error(exc) from exc


@app.post("/api/lots/{lot_id}/quotes", dependencies=[Depends(require_api_key)], status_code=201)
def add_quote(lot_id: int, data: QuoteCreate):
    try:
        return service.add_quote(lot_id, data)
    except Exception as exc:
        raise handle_domain_error(exc) from exc


@app.get("/api/lots/{lot_id}/quotes", dependencies=[Depends(require_api_key)])
def list_quotes(lot_id: int):
    try:
        return service.list_quotes(lot_id)
    except Exception as exc:
        raise handle_domain_error(exc) from exc


@app.get("/api/lots/{lot_id}/comparison", dependencies=[Depends(require_api_key)])
def comparison(lot_id: int):
    try:
        return service.comparison(lot_id)
    except Exception as exc:
        raise handle_domain_error(exc) from exc


@app.get("/api/lots/{lot_id}/price-benchmark", dependencies=[Depends(require_api_key)])
def price_benchmark(lot_id: int):
    try:
        return service.lot_price_benchmark(lot_id)
    except Exception as exc:
        raise handle_domain_error(exc) from exc


@app.post("/api/price-history", dependencies=[Depends(require_api_key)], status_code=201)
def add_price_history(data: PurchaseHistoryCreate):
    try:
        return service.add_purchase_history(data)
    except Exception as exc:
        raise handle_domain_error(exc) from exc


@app.get("/api/price-history", dependencies=[Depends(require_api_key)])
def list_price_history(
    search: str = Query(default=""),
    supplier_id: int | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=500),
):
    try:
        return service.list_purchase_history(search=search, supplier_id=supplier_id, limit=limit)
    except Exception as exc:
        raise handle_domain_error(exc) from exc


@app.get("/api/templates", dependencies=[Depends(require_api_key)])
def list_templates():
    return service.list_templates()


@app.put("/api/templates/{code}", dependencies=[Depends(require_api_key)])
def upsert_template(code: str, data: TemplateUpsert):
    try:
        return service.upsert_template(code, data)
    except Exception as exc:
        raise handle_domain_error(exc) from exc


@app.get("/api/audit", dependencies=[Depends(require_api_key)])
def list_audit(limit: int = Query(default=50, ge=1, le=200)):
    try:
        return service.list_audit(limit)
    except Exception as exc:
        raise handle_domain_error(exc) from exc
