from __future__ import annotations

import hmac
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import (
    Cookie,
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import HTMLResponse, RedirectResponse

from .auth import TokenError, issue_token, verify_token
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
from .passwords import verify_password
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
    version="0.5.0",
    description="Internal supplier RFQ and tender comparison workflow",
    lifespan=lifespan,
)


SESSION_COOKIE = "procurement_session"
SESSION_ISSUER = "das-procurement-agent"
SESSION_AUDIENCE = "das-procurement-web"
LOGIN_WINDOW_SECONDS = 15 * 60
LOGIN_MAX_FAILURES = 5
_LOGIN_FAILURES: dict[str, list[float]] = {}
_LOGIN_LOCK = threading.Lock()


def _session_claims(session_token: str) -> dict[str, Any] | None:
    if not settings.auth_secret or not session_token:
        return None
    try:
        return verify_token(
            settings.auth_secret,
            session_token,
            issuer=SESSION_ISSUER,
            audience=SESSION_AUDIENCE,
            kind="session",
            max_ttl_seconds=settings.session_ttl_seconds,
        )
    except TokenError:
        return None


def _login_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _recent_failures(key: str, now: float) -> list[float]:
    cutoff = now - LOGIN_WINDOW_SECONDS
    with _LOGIN_LOCK:
        recent = [stamp for stamp in _LOGIN_FAILURES.get(key, []) if stamp >= cutoff]
        if recent:
            _LOGIN_FAILURES[key] = recent
        else:
            _LOGIN_FAILURES.pop(key, None)
        return recent


def _record_login_failure(key: str, now: float) -> None:
    with _LOGIN_LOCK:
        failures = _LOGIN_FAILURES.setdefault(key, [])
        failures.append(now)
        if len(_LOGIN_FAILURES) > 2048:
            _LOGIN_FAILURES.pop(next(iter(_LOGIN_FAILURES)))


def _clear_login_failures(key: str) -> None:
    with _LOGIN_LOCK:
        _LOGIN_FAILURES.pop(key, None)


def _login_page(*, error: str = "", status_code: int = 200) -> HTMLResponse:
    path = Path(__file__).parent / "static" / "login.html"
    message = (
        '<div class="error" role="alert">Неверный логин или пароль.</div>'
        if error == "invalid"
        else '<div class="error" role="alert">Слишком много попыток. Повторите позже.</div>'
        if error == "limited"
        else ""
    )
    response = HTMLResponse(
        path.read_text(encoding="utf-8").replace("{{ERROR_MESSAGE}}", message),
        status_code=status_code,
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


def require_access(
    x_api_key: str = Header(default=""),
    session_token: str = Cookie(default="", alias=SESSION_COOKIE),
) -> None:
    if (
        settings.api_key
        and x_api_key
        and hmac.compare_digest(x_api_key, settings.api_key)
    ):
        return
    if _session_claims(session_token):
        return
    if (
        not settings.api_key
        and not settings.local_auth_configured
        and settings.environment != "production"
    ):
        return
    raise HTTPException(status_code=403, detail="access denied")


def handle_domain_error(exc: Exception) -> HTTPException:
    if isinstance(exc, NotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, ConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=422, detail=str(exc))


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "das-procurement-agent", "outbox": settings.outbox_mode}


@app.get("/login", response_class=HTMLResponse, include_in_schema=False)
def login_page(
    session_token: str = Cookie(default="", alias=SESSION_COOKIE),
):
    if not settings.local_auth_configured:
        raise HTTPException(
            status_code=404, detail="local authentication is not configured"
        )
    if _session_claims(session_token):
        return RedirectResponse(url="/", status_code=303)
    return _login_page()


@app.post("/auth/login", include_in_schema=False)
def login(
    request: Request,
    username: str = Form(..., min_length=1, max_length=128),
    password: str = Form(..., min_length=1, max_length=256),
):
    if not settings.local_auth_configured:
        raise HTTPException(
            status_code=404, detail="local authentication is not configured"
        )

    now = time.time()
    key = _login_key(request)
    if len(_recent_failures(key, now)) >= LOGIN_MAX_FAILURES:
        response = _login_page(error="limited", status_code=429)
        response.headers["Retry-After"] = str(LOGIN_WINDOW_SECONDS)
        return response

    username_ok = hmac.compare_digest(username, settings.admin_username)
    password_ok = verify_password(password, settings.admin_password_hash)
    if not (username_ok and password_ok):
        _record_login_failure(key, now)
        return _login_page(error="invalid", status_code=403)

    _clear_login_failures(key)
    session_token = issue_token(
        settings.auth_secret,
        issuer=SESSION_ISSUER,
        audience=SESSION_AUDIENCE,
        subject=settings.admin_username,
        role="admin",
        kind="session",
        ttl_seconds=settings.session_ttl_seconds,
    )
    db.audit(
        "local_login",
        "session",
        settings.admin_username,
        actor=settings.admin_username,
        details={"role": "admin"},
    )
    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie(
        SESSION_COOKIE,
        session_token,
        max_age=settings.session_ttl_seconds,
        httponly=True,
        secure=settings.environment == "production",
        samesite="lax",
        path="/",
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@app.post("/auth/logout", include_in_schema=False)
def logout(
    session_token: str = Cookie(default="", alias=SESSION_COOKIE),
) -> RedirectResponse:
    claims = _session_claims(session_token)
    if claims:
        db.audit(
            "local_logout",
            "session",
            str(claims["sub"]),
            actor=str(claims["sub"]),
            details={},
        )
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@app.get("/", response_class=HTMLResponse)
def index(
    session_token: str = Cookie(default="", alias=SESSION_COOKIE),
):
    if settings.local_auth_configured and not _session_claims(session_token):
        return RedirectResponse(url="/login", status_code=303)
    path = Path(__file__).parent / "static" / "index.html"
    response = HTMLResponse(path.read_text(encoding="utf-8"))
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/api/dashboard", dependencies=[Depends(require_access)])
def dashboard():
    return service.dashboard()


@app.post("/api/projects", dependencies=[Depends(require_access)], status_code=201)
def create_project(data: ProjectCreate):
    try:
        return service.create_project(data)
    except Exception as exc:
        raise handle_domain_error(exc) from exc


@app.get("/api/projects", dependencies=[Depends(require_access)])
def list_projects():
    return service.list_projects()


@app.get("/api/projects/{project_id}", dependencies=[Depends(require_access)])
def get_project(project_id: int):
    try:
        return service.get_project(project_id)
    except Exception as exc:
        raise handle_domain_error(exc) from exc


@app.post("/api/projects/{project_id}/sections", dependencies=[Depends(require_access)], status_code=201)
def add_section(project_id: int, data: SectionCreate):
    try:
        return service.add_section(project_id, data)
    except Exception as exc:
        raise handle_domain_error(exc) from exc


@app.post("/api/suppliers", dependencies=[Depends(require_access)], status_code=201)
def create_supplier(data: SupplierCreate):
    try:
        return service.create_supplier(data)
    except Exception as exc:
        raise handle_domain_error(exc) from exc


@app.get("/api/suppliers", dependencies=[Depends(require_access)])
def list_suppliers(region: str = Query(default=""), category: str = Query(default="")):
    return service.list_suppliers(region=region, category=category)


@app.post("/api/suppliers/import", dependencies=[Depends(require_access)])
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


@app.post("/api/documents", dependencies=[Depends(require_access)], status_code=201)
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


@app.get("/api/documents", dependencies=[Depends(require_access)])
def list_source_documents(extraction_status: str = Query(default="")):
    rows = service.list_source_documents(extraction_status)
    return [{**row, "storage_path": "internal"} for row in rows]


@app.post(
    "/api/documents/{document_id}/procurement-suggestions",
    dependencies=[Depends(require_access)],
    status_code=201,
)
def register_procurement_suggestions(document_id: int, data: ProcurementSuggestionBatch):
    try:
        return service.register_procurement_suggestions(document_id, data.suggestions)
    except Exception as exc:
        raise handle_domain_error(exc) from exc


@app.get("/api/procurement-suggestions", dependencies=[Depends(require_access)])
def list_procurement_suggestions(
    project_id: int | None = Query(default=None), status: str = Query(default="")
):
    try:
        return service.list_procurement_suggestions(project_id=project_id, status=status)
    except Exception as exc:
        raise handle_domain_error(exc) from exc


@app.post(
    "/api/procurement-suggestions/{suggestion_id}/approve",
    dependencies=[Depends(require_access)],
)
def approve_procurement_suggestion(suggestion_id: int, data: ProcurementSuggestionApproval):
    try:
        return service.approve_procurement_suggestion(suggestion_id, data)
    except Exception as exc:
        raise handle_domain_error(exc) from exc


@app.post(
    "/api/procurement-suggestions/{suggestion_id}/reject",
    dependencies=[Depends(require_access)],
)
def reject_procurement_suggestion(suggestion_id: int, data: ProcurementSuggestionRejection):
    try:
        return service.reject_procurement_suggestion(suggestion_id, data)
    except Exception as exc:
        raise handle_domain_error(exc) from exc


@app.post("/api/lots", dependencies=[Depends(require_access)], status_code=201)
def create_lot(data: LotCreate):
    try:
        return service.create_lot(data)
    except Exception as exc:
        raise handle_domain_error(exc) from exc


@app.get("/api/lots", dependencies=[Depends(require_access)])
def list_lots():
    return service.list_lots()


@app.get("/api/lots/{lot_id}", dependencies=[Depends(require_access)])
def get_lot(lot_id: int):
    try:
        return service.get_lot(lot_id)
    except Exception as exc:
        raise handle_domain_error(exc) from exc


@app.get("/api/lots/{lot_id}/supplier-matches", dependencies=[Depends(require_access)])
def supplier_matches(lot_id: int):
    try:
        return service.match_suppliers(lot_id)
    except Exception as exc:
        raise handle_domain_error(exc) from exc


@app.post("/api/lots/{lot_id}/campaigns", dependencies=[Depends(require_access)], status_code=201)
def create_campaign(lot_id: int, data: CampaignCreate):
    try:
        return service.create_campaign(lot_id, data)
    except Exception as exc:
        raise handle_domain_error(exc) from exc


@app.get("/api/campaigns", dependencies=[Depends(require_access)])
def list_campaigns(lot_id: int | None = Query(default=None)):
    try:
        return service.list_campaigns(lot_id)
    except Exception as exc:
        raise handle_domain_error(exc) from exc


@app.get("/api/outbox", dependencies=[Depends(require_access)])
def list_outbox(
    status: str = Query(default=""),
    lot_id: int | None = Query(default=None),
):
    try:
        return service.list_outbox(status=status, lot_id=lot_id)
    except Exception as exc:
        raise handle_domain_error(exc) from exc


@app.post("/api/outbox/{message_id}/approve", dependencies=[Depends(require_access)])
def approve_message(message_id: int, decision: ApprovalDecision):
    try:
        return service.approve_message(message_id, decision.approved_by, decision.comment)
    except Exception as exc:
        raise handle_domain_error(exc) from exc


@app.post("/api/lots/{lot_id}/quotes", dependencies=[Depends(require_access)], status_code=201)
def add_quote(lot_id: int, data: QuoteCreate):
    try:
        return service.add_quote(lot_id, data)
    except Exception as exc:
        raise handle_domain_error(exc) from exc


@app.get("/api/lots/{lot_id}/quotes", dependencies=[Depends(require_access)])
def list_quotes(lot_id: int):
    try:
        return service.list_quotes(lot_id)
    except Exception as exc:
        raise handle_domain_error(exc) from exc


@app.get("/api/lots/{lot_id}/comparison", dependencies=[Depends(require_access)])
def comparison(lot_id: int):
    try:
        return service.comparison(lot_id)
    except Exception as exc:
        raise handle_domain_error(exc) from exc


@app.get("/api/lots/{lot_id}/price-benchmark", dependencies=[Depends(require_access)])
def price_benchmark(lot_id: int):
    try:
        return service.lot_price_benchmark(lot_id)
    except Exception as exc:
        raise handle_domain_error(exc) from exc


@app.post("/api/price-history", dependencies=[Depends(require_access)], status_code=201)
def add_price_history(data: PurchaseHistoryCreate):
    try:
        return service.add_purchase_history(data)
    except Exception as exc:
        raise handle_domain_error(exc) from exc


@app.get("/api/price-history", dependencies=[Depends(require_access)])
def list_price_history(
    search: str = Query(default=""),
    supplier_id: int | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=500),
):
    try:
        return service.list_purchase_history(search=search, supplier_id=supplier_id, limit=limit)
    except Exception as exc:
        raise handle_domain_error(exc) from exc


@app.get("/api/templates", dependencies=[Depends(require_access)])
def list_templates():
    return service.list_templates()


@app.put("/api/templates/{code}", dependencies=[Depends(require_access)])
def upsert_template(code: str, data: TemplateUpsert):
    try:
        return service.upsert_template(code, data)
    except Exception as exc:
        raise handle_domain_error(exc) from exc


@app.get("/api/audit", dependencies=[Depends(require_access)])
def list_audit(limit: int = Query(default=50, ge=1, le=200)):
    try:
        return service.list_audit(limit)
    except Exception as exc:
        raise handle_domain_error(exc) from exc
