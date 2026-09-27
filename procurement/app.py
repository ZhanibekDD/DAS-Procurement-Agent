from __future__ import annotations

import hmac
import hashlib
import json
import os
import asyncio
import html
import re
import sqlite3
import threading
import time
from contextlib import asynccontextmanager
from contextlib import AsyncExitStack
from ipaddress import ip_address, ip_network
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
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, FileResponse
from starlette.concurrency import run_in_threadpool
from pydantic import Field

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

    BatchImportConfirm,
    SupplierDraftConfirm,
    SupplierDraftReject,
    StrictModel,
)
from .launch_workflow import LaunchWorkflow
from .table_ingest import MAX_FILE, read_table
from .upload_io import staged_upload, UploadBodyLimit, upload_request, UploadTooLarge, MAX_BATCH
from .passwords import verify_password
from .service import ConflictError, NotFoundError, ProcurementService
from .identity import authenticated_actor, trusted_actor
from . import sso


settings = Settings.from_env()
db = Database(settings.db_path)
service = ProcurementService(db)
launch = LaunchWorkflow(service)


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.initialize()
    yield


app = FastAPI(
    title="DAS Снабжение",
    version="0.8.0",
    description="Internal supplier RFQ and tender comparison workflow",
    lifespan=lifespan,
)
app.add_middleware(UploadBodyLimit)


SESSION_COOKIE = "procurement_session"
SESSION_ISSUER = "das-procurement-agent"
SESSION_AUDIENCE = "das-procurement-web"
LOGIN_WINDOW_SECONDS = 15 * 60
LOGIN_MAX_FAILURES = 5
_LOGIN_FAILURES: dict[str, list[float]] = {}
_LOGIN_LOCK = threading.Lock()


def _require_active_module_session(principal):
    if principal["session_exp"] <= int(time.time()):
        raise sso.SSOError(401)
    try:
        if db.sso_session_revoked(sso.module_session_key(settings, principal)):
            raise sso.SSOError(401)
    except sqlite3.Error:
        raise sso.SSOError(503) from None


def _set_sso_session(response, principal):
    _require_active_module_session(principal)
    name, _ = sso.cookie_names(settings)
    response.set_cookie(name, sso.session_cookie(settings, principal),
        max_age=min(principal["expires"], principal["session_exp"] - int(time.time())),
        secure=True, httponly=True, samesite="lax", path="/")


_READ_ONLY_GET = (
    r"/", r"/api/auth/session", r"/api/dashboard", r"/api/projects(?:/\d+)?", r"/api/suppliers",
    r"/api/documents", r"/api/procurement-suggestions", r"/api/procurement-suggestions/\d+/reference-checks",
    r"/api/lots(?:/\d+(?:/(?:supplier-matches|quotes|comparison|price-benchmark))?)?",
    r"/api/campaigns", r"/api/outbox", r"/api/price-history", r"/api/templates", r"/api/audit",
    r"/api/imports(?:/\d+)?", r"/api/supplier-drafts", r"/api/price-history-entries", r"/assets/[^/]+",
    r"/api/launch/config", r"/api/launch/suppliers(?:/\d+)?", r"/api/launch/imports",
    r"/api/launch/documents/\d+/download",
)


@app.middleware("http")
async def das_identity_boundary(request: Request, call_next):
    if not settings.sso_enabled:
        if request.url.path in {"/auth/sso", "/auth/sso/callback", "/auth/logged-out", "/api/auth/session"}:
            return JSONResponse({"detail": "not found"}, status_code=404)
        claims = _session_claims(request.cookies.get(SESSION_COOKIE, ''))
        actor = claims['sub'] if claims else None
        if not actor and settings.api_key and hmac.compare_digest(request.headers.get('x-api-key',''),settings.api_key):
            actor = 'service-api'
        if upload_request(request.scope) and not actor and (settings.environment=='production' or settings.api_key or settings.local_auth_configured):
            return JSONResponse({'detail':'access denied'},status_code=403)
        context = authenticated_actor.set(actor)
        try:
            return await call_next(request)
        finally:
            authenticated_actor.reset(context)
    path = request.url.path
    if path in {"/login", "/auth/login"}:
        return JSONResponse({"detail": "local login is disabled; use DAS SSO"}, status_code=404)
    public = {("GET", "/health"), ("GET", "/auth/sso"), ("POST", "/auth/sso/callback"),
              ("GET", "/auth/logged-out")}
    if (request.method, path) in public:
        return await call_next(request)
    session_name, _ = sso.cookie_names(settings)
    cookie = request.cookies.get(session_name, "")
    if not cookie and path == "/" and request.method == "GET":
        return RedirectResponse("/auth/sso", status_code=303)
    try:
        # Check the signed stable module identity before and after backchannel I/O.
        # A late rotating-token response cannot re-authorize a logged-out session.
        _require_active_module_session(sso.session_claims(settings, cookie))
        principal = await asyncio.to_thread(sso.authenticate, settings, cookie)
        _require_active_module_session(principal)
        request.state.das_principal = principal
        safe_method = request.method in {"GET", "HEAD", "OPTIONS"}
        if principal["read_only"] and path != "/auth/logout":
            if not safe_method or not any(re.fullmatch(pattern, path) for pattern in _READ_ONLY_GET):
                raise sso.SSOError(403)
        if not safe_method:
            csrf = request.headers.get("x-csrf-token", "")
            if path == "/auth/logout" and not csrf:
                csrf = str((await request.form()).get("csrf_token", ""))
            if (request.headers.get("origin") != sso.origin(settings.sso_redirect_uri)
                    or not re.fullmatch(r"[A-Za-z0-9_-]{40,128}", csrf)
                    or not hmac.compare_digest(principal["csrf"], csrf)):
                raise sso.SSOError(403)
        context = authenticated_actor.set(principal["sub"])
        try:
            response = await call_next(request)
        finally:
            authenticated_actor.reset(context)
        if not getattr(request.state, "sso_logout", False):
            _set_sso_session(response, principal)
        response.headers["Cache-Control"] = "no-store"
        # Form-bearing UI must preserve Origin on POST, including same-site SSO
        # across ports. Do not permit Origin:null or relax the CSRF boundary.
        response.headers["Referrer-Policy"] = (
            "strict-origin" if path == "/" and response.headers.get("content-type", "").split(";")[0] == "text/html"
            else "no-referrer"
        )
        return response
    except sso.SSOError as exc:
        response = JSONResponse({"detail": str(exc)}, status_code=exc.status,
                                headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
        if exc.status == 401:
            response.delete_cookie(session_name, path="/", secure=True, httponly=True, samesite="lax")
        return response


@app.get("/auth/sso", include_in_schema=False)
def sso_start():
    if not settings.sso_enabled:
        raise HTTPException(status_code=404)
    target, state_cookie = sso.begin(settings)
    response = RedirectResponse(target, status_code=303)
    _, name = sso.cookie_names(settings)
    response.set_cookie(name, state_cookie, max_age=300, secure=True, httponly=True,
                        samesite="lax", path="/auth/sso/callback")
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@app.post("/auth/sso/callback", include_in_schema=False)
async def sso_callback(request: Request, code: str = Form(..., min_length=16, max_length=8192),
                       state: str = Form(..., min_length=32, max_length=128)):
    if not settings.sso_enabled:
        raise HTTPException(status_code=404)
    _, name = sso.cookie_names(settings)
    try:
        if request.headers.get("origin") != sso.origin(settings.sso_authorize_url):
            raise sso.SSOError(403)
        principal = await asyncio.to_thread(sso.complete, settings, request.cookies.get(name, ""), code, state)
        response = RedirectResponse("/", status_code=303)
        _set_sso_session(response, principal)
        db.audit("sso_login", "session", principal["sub"], actor=principal["sub"])
    except sso.SSOError as exc:
        response = JSONResponse({"detail": str(exc)}, status_code=exc.status)
    response.delete_cookie(name, path="/auth/sso/callback", secure=True, httponly=True, samesite="lax")
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@app.get("/auth/logged-out", response_class=HTMLResponse, include_in_schema=False)
def sso_logged_out():
    if not settings.sso_enabled:
        raise HTTPException(status_code=404)
    return HTMLResponse('<p>Сеанс Снабжения завершён.</p><a href="/auth/sso">Войти через ДАС</a>',
                        headers={"Cache-Control": "no-store"})


@app.get("/api/auth/session", include_in_schema=False)
def identity_session(request: Request):
    principal = getattr(request.state, "das_principal", None)
    if not settings.sso_enabled or not principal:
        raise HTTPException(status_code=404)
    return {key: principal[key] for key in ("sub", "username", "email", "read_only", "csrf")}


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


def _client_ip(request: Request) -> str:
    peer = request.client.host if request.client else "unknown"
    try:
        peer_address = ip_address(peer)
    except ValueError:
        return peer

    trusted_networks = tuple(
        ip_network(value, strict=False) for value in settings.trusted_proxy_networks
    )
    if not any(peer_address in network for network in trusted_networks):
        return str(peer_address)

    forwarded = request.headers.get("x-forwarded-for", "")
    if not forwarded:
        return str(peer_address)
    try:
        chain = [ip_address(value.strip()) for value in forwarded.split(",")]
    except ValueError:
        return str(peer_address)

    for address in reversed(chain):
        if not any(address in network for network in trusted_networks):
            return str(address)
    return str(chain[0])


def _login_keys(request: Request, username: str) -> tuple[str, str]:
    return (
        f"ip:{_client_ip(request)}",
        f"account:{username.casefold()}",
    )


def _recent_failures(key: str, now: float) -> list[float]:
    cutoff = now - LOGIN_WINDOW_SECONDS
    with _LOGIN_LOCK:
        recent = [stamp for stamp in _LOGIN_FAILURES.get(key, []) if stamp >= cutoff]
        if recent:
            _LOGIN_FAILURES[key] = recent
        else:
            _LOGIN_FAILURES.pop(key, None)
        return recent


def _record_login_failure(keys: tuple[str, ...], now: float) -> None:
    with _LOGIN_LOCK:
        for key in keys:
            _LOGIN_FAILURES.setdefault(key, []).append(now)
        while len(_LOGIN_FAILURES) > 2048:
            _LOGIN_FAILURES.pop(next(iter(_LOGIN_FAILURES)))


def _clear_login_failures(keys: tuple[str, ...]) -> None:
    with _LOGIN_LOCK:
        for key in keys:
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
    response.headers["Referrer-Policy"] = "strict-origin"
    return response


def require_access(
    request: Request,
    x_api_key: str = Header(default=""),
    session_token: str = Cookie(default="", alias=SESSION_COOKIE),
) -> None:
    if settings.sso_enabled:
        if getattr(request.state, "das_principal", None):
            return
        raise HTTPException(status_code=403, detail="DAS SSO access required")
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
    if isinstance(exc, UploadTooLarge):
        return HTTPException(status_code=413, detail=str(exc))
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
    keys = _login_keys(request, username)
    limited = any(
        len(_recent_failures(key, now)) >= LOGIN_MAX_FAILURES for key in keys
    )
    username_ok = hmac.compare_digest(username, settings.admin_username)
    password_ok = verify_password(password, settings.admin_password_hash)
    if username_ok and password_ok:
        _clear_login_failures(keys)
    elif limited:
        response = _login_page(error="limited", status_code=429)
        response.headers["Retry-After"] = str(LOGIN_WINDOW_SECONDS)
        return response
    else:
        _record_login_failure(keys, now)
        return _login_page(error="invalid", status_code=403)

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
    request: Request,
    session_token: str = Cookie(default="", alias=SESSION_COOKIE),
) -> RedirectResponse:
    if settings.sso_enabled:
        principal = request.state.das_principal
        try:
            db.revoke_sso_session(sso.module_session_key(settings, principal), principal["session_exp"],
                                  principal["sub"], int(time.time()))
        except sqlite3.Error:
            raise sso.SSOError(503) from None
        request.state.sso_logout = True
        response = RedirectResponse("/auth/logged-out", status_code=303)
        name, _ = sso.cookie_names(settings)
        response.delete_cookie(name, path="/", secure=True, httponly=True, samesite="lax")
        return response
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
    request: Request,
    session_token: str = Cookie(default="", alias=SESSION_COOKIE),
):
    if settings.local_auth_configured and not _session_claims(session_token):
        return RedirectResponse(url="/login", status_code=303)
    path = Path(__file__).parent / "static" / "index.html"
    content = path.read_text(encoding="utf-8")
    # A no-store HTML page must not reuse yesterday's cached workflow script.
    launch_digest = hashlib.sha256((path.parent / "launch.js").read_bytes()).hexdigest()
    content = content.replace('src="/assets/launch.js"',
                              'src="/assets/launch.js?v=' + launch_digest + '"')
    if not settings.sso_enabled and session_token:
        csrf = hmac.new(settings.auth_secret.encode(), ('launch:' + session_token).encode(), hashlib.sha256).hexdigest()
        content = content.replace('<head>', '<head><meta name="procurement-launch-csrf" content="' + csrf + '">')
    if settings.sso_enabled:
        principal = request.state.das_principal
        content = content.replace('Независимая учётная запись «Снабжения». API-ключ в браузер не передаётся.',
                                  'Личная учётная запись ДАС. Права проверяются сервером.')
        content = content.replace('<form method="post" action="/auth/logout">',
            '<form method="post" action="/auth/logout"><input type="hidden" name="csrf_token" value="'
            + html.escape(principal["csrf"], quote=True) + '">')
        content = content.replace('<head>', '<head><meta name="procurement-csrf" content="'
                                  + html.escape(principal["csrf"], quote=True) + '">')
    response = HTMLResponse(content)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "strict-origin"
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
        from .table_ingest import contacts
        checked = contacts({'email': data.email, 'phone': data.phone})
        data = data.model_copy(update={k: checked[k] for k in ('email','phone')})
        return service.create_supplier(data)
    except Exception as exc:
        raise handle_domain_error(exc) from exc


@app.get("/api/suppliers", dependencies=[Depends(require_access)])
def list_suppliers(region: str = Query(default=""), category: str = Query(default="")):
    return service.list_suppliers(region=region, category=category)


@app.post("/api/suppliers/import", dependencies=[Depends(require_access)])
async def import_suppliers(file: UploadFile = File(...), commit: bool = Query(default=False)):
    try:
        async with staged_upload(file) as content:
            preview = await run_in_threadpool(parse_supplier_table,content,file.filename or '')
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
        async with staged_upload(file) as content:
            result = await run_in_threadpool(service.register_source_document,
                filename=file.filename or '',content=content,document_type=document_type,
                content_type=file.content_type or 'application/octet-stream',project_id=project_id,supplier_id=supplier_id)
        return {**result, "storage_path": "internal", "next_step": "ai_extraction_then_human_review"}
    except Exception as exc:
        raise handle_domain_error(exc) from exc


@app.get("/api/documents", dependencies=[Depends(require_access)])
def list_source_documents(extraction_status: str = Query(default="")):
    rows = service.list_source_documents(extraction_status)
    return [{**row, "storage_path": "internal"} for row in rows]


@app.post(
    "/api/documents/{document_id}/extract/fence-schedule",
    dependencies=[Depends(require_access)],
    status_code=201,
)
def extract_fence_schedule(
    document_id: int,
    page: int = Query(..., ge=1, le=10000),
):
    try:
        return service.analyze_fence_schedule(document_id, page_number=page)
    except Exception as exc:
        raise handle_domain_error(exc) from exc


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
    "/api/procurement-suggestions/{suggestion_id}/reference-checks/{reference_document_id}",
    dependencies=[Depends(require_access)],
)
def check_procurement_reference(suggestion_id: int, reference_document_id: int):
    try:
        return service.check_fence_reference(suggestion_id, reference_document_id)
    except Exception as exc:
        raise handle_domain_error(exc) from exc


@app.get(
    "/api/procurement-suggestions/{suggestion_id}/reference-checks",
    dependencies=[Depends(require_access)],
)
def list_procurement_reference_checks(suggestion_id: int):
    try:
        return service.list_reference_checks(suggestion_id)
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


@app.post("/api/outbox/{message_id}/simulate", dependencies=[Depends(require_access)])
def simulate_outbox(message_id: int):
    try:
        return service.simulate_outbox(message_id)
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


# ── PR #8: batch import & supplier-drafts endpoints ──────────────────────────
MAX_IMPORT_BATCH_BYTES = MAX_BATCH


@app.post("/api/imports/batch", dependencies=[Depends(require_access)], status_code=201)
async def batch_import(
    files: list[UploadFile] = File(...),
    created_by: str = Query(default="system"),
):
    """Upload 1-20 КП/счёт/прайс-лист files; returns batch record with drafts & entries."""
    if not files:
        raise HTTPException(status_code=422, detail="at least one file is required")
    if len(files) > 20:
        raise HTTPException(status_code=422, detail="maximum 20 files per batch")
    try:
        async with AsyncExitStack() as stack:
            file_pairs=[]
            total=0
            for f in files:
                content=await stack.enter_async_context(staged_upload(f,MAX_IMPORT_BATCH_BYTES-total))
                total+=len(content)
                file_pairs.append((f.filename or 'unnamed',content))
            return await run_in_threadpool(service.create_import_batch,file_pairs,created_by=created_by)
    except Exception as exc:
        raise handle_domain_error(exc) from exc


@app.get("/api/imports", dependencies=[Depends(require_access)])
def list_import_batches():
    try:
        return service.list_import_batches()
    except Exception as exc:
        raise handle_domain_error(exc) from exc


@app.get("/api/imports/{batch_id}", dependencies=[Depends(require_access)])
def get_import_batch(batch_id: int):
    try:
        return service.get_import_batch(batch_id)
    except Exception as exc:
        raise handle_domain_error(exc) from exc


@app.post("/api/imports/{batch_id}/confirm", dependencies=[Depends(require_access)])
def confirm_batch(batch_id: int, data: BatchImportConfirm):
    try:
        return service.confirm_batch_entries(batch_id, data.entry_ids, data.confirmed_by)
    except Exception as exc:
        raise handle_domain_error(exc) from exc


@app.get("/api/supplier-drafts", dependencies=[Depends(require_access)])
def list_supplier_drafts(
    status: str = Query(default="needs_review"),
    batch_id: int | None = Query(default=None),
):
    try:
        return service.list_supplier_drafts(status=status, batch_id=batch_id)
    except Exception as exc:
        raise handle_domain_error(exc) from exc


@app.post("/api/supplier-drafts/{draft_id}/confirm", dependencies=[Depends(require_access)])
def confirm_supplier_draft(draft_id: int, data: SupplierDraftConfirm):
    try:
        return service.confirm_supplier_draft(draft_id, data)
    except Exception as exc:
        raise handle_domain_error(exc) from exc


@app.post("/api/supplier-drafts/{draft_id}/reject", dependencies=[Depends(require_access)])
def reject_supplier_draft(draft_id: int, data: SupplierDraftReject):
    try:
        return service.reject_supplier_draft(draft_id, data)
    except Exception as exc:
        raise handle_domain_error(exc) from exc


@app.get("/api/price-history-entries", dependencies=[Depends(require_access)])
def list_price_history_entries(
    search: str = Query(default=""),
    status: str = Query(default="confirmed"),
    supplier_id: int | None = Query(default=None),
    batch_id: int | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=500),
):
    try:
        return service.list_price_history_entries(
            search=search,
            status=status,
            supplier_id=supplier_id,
            batch_id=batch_id,
            limit=limit,
        )
    except Exception as exc:
        raise handle_domain_error(exc) from exc


from .launch_routes import install as install_launch_routes
install_launch_routes(app, settings, service, launch, require_access, _session_claims, handle_domain_error)
