"""SSO acceptance against a mock DAS authority; no credentials or live endpoints."""
from __future__ import annotations

import base64
import hashlib
import secrets
import time
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

import procurement.app as application
from procurement import sso
from procurement.config import Settings
from procurement.db import Database
from procurement.service import ProcurementService

ALICE = "00000000-0000-4000-8000-000000000001"
BOB = "00000000-0000-4000-8000-000000000002"
BASE = "https://procurement.test:9206"
DAS = "https://procurement.test:9444"


class Authority:
    def __init__(self):
        self.users = {sub: {"epoch": 1, "active": True, "read_only": False,
                            "access_admin": False, "modules": ["procurement"]}
                      for sub in (ALICE, BOB)}
        self.tokens = {}
        self.codes = {}
        self.calls = []
        self.outage = False
        self.bad_field = None

    def issue(self, sub):
        token = secrets.token_urlsafe(32)
        self.tokens[token] = (sub, self.users[sub]["epoch"])
        return token

    def authorize(self, query, sub):
        code = secrets.token_urlsafe(32)
        self.codes[code] = (sub, query["code_challenge"][0], query["redirect_uri"][0])
        return code

    def post(self, settings, endpoint, payload):
        self.calls.append(endpoint)
        if self.outage:
            raise sso.SSOError(503)
        assert payload["client_id"] == "procurement"
        if endpoint == "/access/sso/token/":
            code = payload["code"]
            if code not in self.codes:
                raise sso.SSOError(403)
            sub, challenge, redirect = self.codes.pop(code)
            actual = base64.urlsafe_b64encode(hashlib.sha256(payload["code_verifier"].encode()).digest()).rstrip(b"=").decode()
            if actual != challenge or payload["redirect_uri"] != redirect:
                raise sso.SSOError(403)
            return {"access_token": self.issue(sub), "expires_in": 120}
        binding = self.tokens.get(payload["token"])
        if not binding:
            return {"active": False}
        sub, epoch = binding
        user = self.users[sub]
        if not user["active"] or user["epoch"] != epoch:
            return {"active": False}
        result = {"active": True, "sub": sub, "epoch": epoch,
            "username": "alice" if sub == ALICE else "bob", "email": "test@example.invalid",
            "modules": user["modules"], "read_only": user["read_only"],
            "access_admin": user["access_admin"],
            "access_token": self.issue(sub), "expires_in": 120}
        if self.bad_field:
            result.pop(self.bad_field)
        return result


@pytest.fixture
def boundary(tmp_path, monkeypatch):
    settings = Settings(environment="canary", api_key="legacy-test-api-key", db_path=str(tmp_path / "sso.db"),
        outbox_mode="draft_only", auth_secret="state-signing-test-" + "s" * 32,
        admin_username="", admin_password_hash="", session_ttl_seconds=3600,
        sso_enabled=True, sso_authorize_url=DAS + "/access/sso/authorize/",
        sso_internal_base_url="http://das-identity.test:8000", sso_client_id="procurement",
        sso_client_secret="service-only-test-" + "x" * 32, sso_redirect_uri=BASE + "/auth/sso/callback")
    database = Database(settings.db_path)
    database.initialize()
    authority = Authority()
    monkeypatch.setattr(application, "settings", settings)
    monkeypatch.setattr(application, "db", database)
    monkeypatch.setattr(application, "service", ProcurementService(database))
    monkeypatch.setattr(sso, "_post", authority.post)
    with TestClient(application.app, base_url=BASE) as client:
        yield client, authority, settings, database


def login(client, authority, sub=ALICE):
    start = client.get("/auth/sso", follow_redirects=False)
    assert start.status_code == 303
    query = parse_qs(urlsplit(start.headers["location"]).query)
    code = authority.authorize(query, sub)
    done = client.post("/auth/sso/callback", data={"code": code, "state": query["state"][0]},
                       headers={"Origin": DAS}, follow_redirects=False)
    assert done.status_code == 303
    return client.get("/api/auth/session").json()


def headers(identity):
    return {"Origin": BASE, "X-CSRF-Token": identity["csrf"]}


def test_sso_exchange_pkce_secure_namespaced_cookies_and_live_introspection(boundary):
    client, authority, settings, _ = boundary
    identity = login(client, authority)
    assert identity["sub"] == ALICE
    session_name, state_name = sso.cookie_names(settings)
    assert client.cookies.get(session_name)
    assert client.cookies.get(state_name) is None
    before = len(authority.calls)
    response = client.get("/api/dashboard")
    assert response.status_code == 200
    assert len(authority.calls) == before + 1
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=lax" in cookie and "Max-Age=120" in cookie
    assert response.headers["cache-control"] == "no-store"
    other = replace(settings, sso_redirect_uri="https://procurement.test:19206/auth/sso/callback")
    assert sso.cookie_names(other) != sso.cookie_names(settings)
    with pytest.raises(sso.SSOError):
        sso.authenticate(other, client.cookies.get(session_name))


def test_admin_role_requires_current_authoritative_backchannel_entitlement(boundary):
    client, authority, _, _ = boundary
    login(client, authority)
    assert client.get('/api/ui-context', headers={'X-OpenWebUI-User-Role': 'admin'}).json() == {'role': 'staff'}
    assert client.get('/api/audit').status_code == 403

    authority.users[ALICE]['access_admin'] = True
    assert client.get('/api/ui-context').json() == {'role': 'admin'}
    assert client.get('/api/audit').status_code == 200

    authority.users[ALICE]['read_only'] = True
    assert client.get('/api/ui-context').json() == {'role': 'staff'}
    assert client.get('/api/audit').status_code == 403

    authority.users[ALICE]['read_only'] = False
    authority.users[ALICE]['access_admin'] = False
    assert client.get('/api/ui-context').json() == {'role': 'staff'}
    assert client.get('/api/audit').status_code == 403


def test_missing_admin_entitlement_fails_closed_and_malformed_claim_is_rejected(boundary, monkeypatch):
    client, authority, settings, _ = boundary
    login(client, authority)
    original = authority.post

    def no_admin_claim(cfg, endpoint, payload):
        result = original(cfg, endpoint, payload)
        if endpoint == '/access/sso/introspect/':
            result.pop('access_admin', None)
        return result

    monkeypatch.setattr(sso, '_post', no_admin_claim)
    assert client.get('/api/ui-context').json() == {'role': 'staff'}
    assert client.get('/api/audit').status_code == 403

    def invalid_admin_claim(cfg, endpoint, payload):
        result = original(cfg, endpoint, payload)
        if endpoint == '/access/sso/introspect/':
            result['access_admin'] = 'true'
        return result

    monkeypatch.setattr(sso, '_post', invalid_admin_claim)
    with pytest.raises(sso.SSOError) as exc:
        sso.introspect(settings, authority.issue(ALICE))
    assert exc.value.status == 503


@pytest.mark.parametrize("mutation", ["state", "unicode_state", "signature", "expiry", "verifier", "origin", "null_origin"])
def test_state_cookie_pkce_and_origin_tampering_fail_closed(boundary, mutation):
    client, authority, settings, _ = boundary
    response = client.get("/auth/sso", follow_redirects=False)
    query = parse_qs(urlsplit(response.headers["location"]).query)
    code = authority.authorize(query, ALICE)
    _, name = sso.cookie_names(settings)
    cookie = client.cookies.get(name)
    state = query["state"][0]
    request_origin = DAS
    if mutation == "state":
        state = "x" * 43
    elif mutation == "unicode_state":
        state = "я" * 43
    elif mutation == "origin":
        request_origin = "https://attacker.invalid"
    elif mutation == "null_origin":
        request_origin = "null"
    elif mutation == "signature":
        cookie = cookie[:-1] + ("A" if cookie[-1] != "A" else "B")
    else:
        saved = sso.unseal(settings, "state", cookie)
        if mutation == "expiry":
            saved["exp"] = int(time.time()) - 1
        else:
            saved["verifier"] = secrets.token_urlsafe(48)
        cookie = sso.seal(settings, "state", saved)
    client.cookies.clear()
    client.cookies.set(name, cookie, domain="procurement.test", path="/auth/sso/callback")
    result = client.post("/auth/sso/callback", data={"state": state, "code": code},
                         headers={"Origin": request_origin}, follow_redirects=False)
    assert result.status_code in {401, 403}
    assert client.get("/api/dashboard").status_code == 401


def test_api_key_legacy_login_identity_headers_and_forged_cookie_cannot_bypass(boundary):
    client, _, settings, _ = boundary
    assert client.get("/api/dashboard", headers={"X-API-Key": settings.api_key, "X-User-ID": ALICE,
        "X-User-Email": "admin@example.invalid"}).status_code == 401
    assert client.get("/login").status_code == 404
    assert client.post("/auth/login", data={"username": "shared", "password": "synthetic-only"}).status_code == 404
    name, _ = sso.cookie_names(settings)
    client.cookies.set(name, "not-a-valid-session", domain="procurement.test", path="/")
    assert client.get("/api/dashboard").status_code == 401


@pytest.mark.parametrize("event", ["password_reset", "revoke_sessions", "block", "remove_module", "outage"])
def test_authority_changes_apply_on_next_request_without_local_cache(boundary, event):
    client, authority, _, _ = boundary
    login(client, authority)
    if event in {"password_reset", "revoke_sessions"}:
        authority.users[ALICE]["epoch"] += 1
    elif event == "block":
        authority.users[ALICE]["active"] = False
    elif event == "remove_module":
        authority.users[ALICE]["modules"] = []
    else:
        authority.outage = True
    assert client.get("/api/dashboard").status_code == (503 if event == "outage" else 403)


@pytest.mark.parametrize("field", ["sub", "username", "email", "modules", "read_only", "epoch", "access_token", "expires_in"])
def test_incomplete_authority_response_is_not_trusted(boundary, field):
    client, authority, _, _ = boundary
    login(client, authority)
    authority.bad_field = field
    assert client.get("/api/dashboard").status_code == 503


def test_wrong_renewed_subject_fails_even_with_valid_authority_response(boundary, monkeypatch):
    client, authority, _, _ = boundary
    login(client, authority)
    original = authority.post
    def swapped(settings, endpoint, payload):
        result = original(settings, endpoint, payload)
        result["sub"] = BOB
        return result
    monkeypatch.setattr(sso, "_post", swapped)
    assert client.get("/api/dashboard").status_code == 403


def test_read_only_denies_writes_and_unclassified_get_without_calling_handler(boundary):
    client, authority, _, database = boundary
    identity = login(client, authority)
    authority.users[ALICE]["read_only"] = True
    assert client.get("/api/suppliers").status_code == 200
    assert client.post("/api/projects", json={"name": "Should not exist"}, headers=headers(identity)).status_code == 403
    assert client.get("/api/future-mutating-get?confirm=true").status_code == 403
    assert database.one("SELECT count(*) AS n FROM projects")["n"] == 0


@pytest.mark.parametrize("csrf_case", ["missing", "wrong", "wrong_origin", "missing_origin", "null_origin"])
def test_cookie_writes_require_csrf_and_exact_origin(boundary, csrf_case):
    client, authority, _, database = boundary
    identity = login(client, authority)
    request_headers = headers(identity)
    if csrf_case == "missing":
        request_headers.pop("X-CSRF-Token")
    elif csrf_case == "wrong":
        request_headers["X-CSRF-Token"] = "forged"
    elif csrf_case == "wrong_origin":
        request_headers["Origin"] = "https://attacker.invalid"
    elif csrf_case == "null_origin":
        request_headers["Origin"] = "null"
    else:
        request_headers.pop("Origin")
    result = client.post("/api/projects", json={"name": "Test", "region": "Воронеж", "delivery_address": "Test address"},
                         headers=request_headers)
    assert result.status_code == 403
    assert database.one("SELECT count(*) AS n FROM projects")["n"] == 0


def test_body_actor_is_replaced_and_each_user_has_distinct_session_csrf(boundary):
    client, authority, _, database = boundary
    alice = login(client, authority)
    with TestClient(application.app, base_url=BASE) as second:
        bob = login(second, authority, BOB)
        assert alice["sub"] != bob["sub"] and alice["csrf"] != bob["csrf"]
        # A same chat_id query parameter and browser identity headers are never an identity source.
        identity = second.get("/api/auth/session?chat_id=same", headers={"X-User-ID": ALICE}).json()
        assert identity["sub"] == BOB
        body = {"item_name": "Synthetic cable", "quantity": "1", "unit": "м", "unit_price": "10",
                "currency": "RUB", "vat_included": True,
                "purchased_on": "2026-09-04", "confirmed_by": "forged-admin"}
        assert second.post("/api/price-history", json=body, headers=headers(alice)).status_code == 403
        result = second.post("/api/price-history", json=body, headers=headers(bob))
        assert result.status_code == 201
        assert result.json()["confirmed_by"] == BOB
    audit = database.one("SELECT actor FROM audit_log WHERE entity_type='purchase_history' ORDER BY id DESC LIMIT 1")
    assert audit["actor"] == BOB
    assert client.get("/api/auth/session?chat_id=same").json()["sub"] == ALICE


def test_root_renders_sso_csrf_and_logout_does_not_reissue_session(boundary):
    client, authority, settings, _ = boundary
    identity = login(client, authority)
    page = client.get("/")
    assert 'name="procurement-csrf"' in page.text
    assert 'name="csrf_token"' in page.text
    assert "Независимая учётная запись" not in page.text
    result = client.post("/auth/logout", data={"csrf_token": identity["csrf"]}, headers={"Origin": BASE}, follow_redirects=False)
    assert result.status_code == 303
    assert client.cookies.get(sso.cookie_names(settings)[0]) is None
    assert client.get("/api/dashboard").status_code == 401


def test_form_ui_preserves_origin_but_json_and_sso_redirects_keep_no_referrer(boundary):
    client, authority, settings, _ = boundary
    start = client.get("/auth/sso", follow_redirects=False)
    assert start.headers["referrer-policy"] == "no-referrer"
    identity = login(client, authority)
    page = client.get("/")
    assert page.headers["referrer-policy"] == "strict-origin"
    assert '<meta name="referrer" content="strict-origin">' in page.text
    assert '<form method="post" action="/auth/logout">' in page.text
    assert 'content="no-referrer"' not in page.text
    assert client.get("/api/auth/session").headers["referrer-policy"] == "no-referrer"
    assert client.get("/api/dashboard").headers["referrer-policy"] == "no-referrer"
    cookie = page.headers["set-cookie"]
    assert "Secure" in cookie and "HttpOnly" in cookie and "SameSite=lax" in cookie
    # A real form POST must present the module origin; null remains a CSRF failure.
    denied = client.post("/auth/logout", data={"csrf_token": identity["csrf"]},
                         headers={"Origin": "null"}, follow_redirects=False)
    assert denied.status_code == 403 and denied.headers["referrer-policy"] == "no-referrer"
    accepted = client.post("/auth/logout", data={"csrf_token": identity["csrf"]},
                           headers={"Origin": BASE}, follow_redirects=False)
    assert accepted.status_code == 303 and accepted.headers["referrer-policy"] == "no-referrer"
    assert client.cookies.get(sso.cookie_names(settings)[0]) is None


def test_expired_module_cookie_fails_before_backchannel(boundary):
    client, authority, settings, _ = boundary
    login(client, authority)
    name, _ = sso.cookie_names(settings)
    payload = sso.unseal(settings, "session", client.cookies.get(name))
    payload["exp"] = int(time.time()) - 1
    client.cookies.clear()
    client.cookies.set(name, sso.seal(settings, "session", payload), domain="procurement.test", path="/")
    before = len(authority.calls)
    assert client.get("/api/dashboard").status_code == 401
    assert len(authority.calls) == before


def test_authorization_code_replay_is_rejected(boundary):
    client, authority, settings, _ = boundary
    result = client.get("/auth/sso", follow_redirects=False)
    query = parse_qs(urlsplit(result.headers["location"]).query)
    _, name = sso.cookie_names(settings)
    saved_cookie = client.cookies.get(name)
    code = authority.authorize(query, ALICE)
    data = {"state": query["state"][0], "code": code}
    assert client.post("/auth/sso/callback", data=data, headers={"Origin": DAS}, follow_redirects=False).status_code == 303
    client.cookies.set(name, saved_cookie, domain="procurement.test", path="/auth/sso/callback")
    assert client.post("/auth/sso/callback", data=data, headers={"Origin": DAS}, follow_redirects=False).status_code == 403


def test_async_import_creator_and_confirmation_actor_are_server_subject(boundary):
    import io
    from openpyxl import Workbook
    client, authority, _, database = boundary
    identity = login(client, authority)
    workbook = Workbook()
    workbook.active.append(["Наименование", "Цена RUB без НДС"])
    workbook.active.append(["Synthetic cable", 10])
    output = io.BytesIO()
    workbook.save(output)
    workbook.close()
    response = client.post("/api/imports/batch?created_by=forged", headers=headers(identity),
                           files={"files": ("synthetic.xlsx", output.getvalue())})
    assert response.status_code == 201
    batch = response.json()
    assert batch["created_by"] == ALICE
    entry_id = batch["price_history_entries"][0]["id"]
    confirmed = client.post(f"/api/imports/{batch['id']}/confirm", headers=headers(identity),
                            json={"confirmed_by": "forged", "entry_ids": [entry_id]})
    assert confirmed.status_code == 200
    assert database.one("SELECT confirmed_by FROM price_history_entries WHERE id=?", (entry_id,))["confirmed_by"] == ALICE
    assert database.one("SELECT actor FROM audit_log WHERE entity_type='import_batch' ORDER BY id DESC LIMIT 1")["actor"] == ALICE


def test_body_identity_fields_are_not_accepted_by_business_models(boundary):
    client, authority, _, database = boundary
    identity = login(client, authority)
    response = client.post("/api/projects", headers=headers(identity), json={"name": "Synthetic project",
        "region": "Воронеж", "delivery_address": "Synthetic address", "user_id": BOB, "email": "forged@example.invalid"})
    assert response.status_code == 422
    assert database.one("SELECT count(*) AS n FROM projects")["n"] == 0


def test_sso_settings_fail_closed_and_do_not_require_local_passwords(monkeypatch):
    for key in list(__import__("os").environ):
        if key.startswith(("PROCUREMENT_", "DAS_SSO_")):
            monkeypatch.delenv(key)
    monkeypatch.setenv("DAS_SSO_CLIENT_ID", "procurement")
    with pytest.raises(RuntimeError):
        Settings.from_env()
    for key, value in {"DAS_SSO_AUTHORIZE_URL": DAS + "/access/sso/authorize/",
        "DAS_SSO_INTERNAL_BASE_URL": "http://das-identity.test:8000",
        "DAS_SSO_CLIENT_SECRET": "test-service-" + "x" * 32,
        "DAS_SSO_REDIRECT_URI": BASE + "/auth/sso/callback",
        "PROCUREMENT_AUTH_SECRET": "test-state-" + "s" * 32, "PROCUREMENT_ENV": "production"}.items():
        monkeypatch.setenv(key, value)
    configured = Settings.from_env()
    assert configured.sso_enabled and not configured.local_auth_configured
    monkeypatch.setenv("DAS_SSO_REDIRECT_URI", "https://different-site.test/auth/sso/callback")
    with pytest.raises(RuntimeError, match="same scheme and hostname"):
        Settings.from_env()
    monkeypatch.setenv("DAS_SSO_REDIRECT_URI", BASE + "/auth/sso/callback")
    monkeypatch.setenv("PROCUREMENT_ADMIN_USERNAME", "shared-admin")
    with pytest.raises(RuntimeError, match="forbids local admin"):
        Settings.from_env()


@pytest.mark.parametrize("length,expected", [(149, 200), (150, 200), (151, 503)])
def test_username_length_matches_das_authority_contract(boundary, monkeypatch, length, expected):
    client, authority, _, _ = boundary
    login(client, authority)
    original = authority.post
    def changed_name(settings, endpoint, payload):
        response = original(settings, endpoint, payload)
        response["username"] = "u" * length
        return response
    monkeypatch.setattr(sso, "_post", changed_name)
    assert client.get("/api/auth/session").status_code == expected


def test_sandbox_api_requires_human_approval_and_uses_trusted_actor(boundary):
    client, authority, _, database = boundary
    identity = login(client, authority)
    request_headers = headers(identity)
    project = client.post("/api/projects", headers=request_headers, json={"name": "Sandbox project",
        "region": "Воронеж", "delivery_address": "Test address"}).json()
    supplier = client.post("/api/suppliers", headers=request_headers, json={"name": "Sandbox supplier",
        "region": "Воронеж", "email": "test@example.invalid"}).json()
    lot = client.post("/api/lots", headers=request_headers, json={"project_id": project["id"],
        "title": "Sandbox lot", "region": "Воронеж", "delivery_address": "Test address",
        "response_deadline": "2099-12-31", "items": [{"name": "Synthetic cable", "quantity": 1, "unit": "м"}]}).json()
    campaign = client.post(f"/api/lots/{lot['id']}/campaigns", headers=request_headers,
                           json={"supplier_ids": [supplier["id"]]}).json()
    message_id = campaign["messages"][0]["id"]
    assert client.post(f"/api/outbox/{message_id}/simulate", headers=request_headers).status_code == 409
    approval = client.post(f"/api/outbox/{message_id}/approve", headers=request_headers,
                           json={"approved_by": "forged-admin"})
    assert approval.status_code == 200 and approval.json()["approved_by"] == ALICE
    response = client.post(f"/api/outbox/{message_id}/simulate", headers=request_headers)
    assert response.status_code == 200 and response.json()["external_send"] is False
    assert database.one("SELECT simulated_by FROM sandbox_deliveries WHERE message_id=?", (message_id,))["simulated_by"] == ALICE
    authority.users[ALICE]["read_only"] = True
    assert client.post(f"/api/outbox/{message_id}/simulate", headers=request_headers).status_code == 403
