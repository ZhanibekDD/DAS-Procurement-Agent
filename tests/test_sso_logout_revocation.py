"""Deterministic logout races; mocked DAS authority, real temporary SQLite storage."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import sqlite3
from threading import Event
import time

import pytest
from fastapi.testclient import TestClient

import procurement.app as application
from procurement import sso
from procurement.db import Database
from test_sso_adapter import ALICE, BOB, BASE, boundary, login


def replay(client, settings, cookie):
    name, _ = sso.cookie_names(settings)
    return client.get('/api/auth/session', headers={'Cookie': f'{name}={cookie}'})


def logout(client, identity):
    return client.post('/auth/logout', data={'csrf_token': identity['csrf']},
                       headers={'Origin': BASE}, follow_redirects=False)


@pytest.mark.parametrize('pause_at', ['introspection', 'endpoint', 'response_cookie'])
def test_inflight_api_cannot_restore_session_after_logout(boundary, monkeypatch, pause_at):
    client, authority, settings, database = boundary
    identity = login(client, authority)
    name, _ = sso.cookie_names(settings)
    old_cookie = client.cookies.get(name)
    entered, release = Event(), Event()

    def pause():
        entered.set()
        assert release.wait(10), 'test coordinator did not release the isolated request'

    if pause_at == 'introspection':
        original = sso.authenticate
        def delayed_auth(config, cookie):
            principal = original(config, cookie)
            # Logout itself must be able to authenticate and commit its revocation.
            if not entered.is_set():
                pause()
            return principal
        monkeypatch.setattr(sso, 'authenticate', delayed_auth)
    elif pause_at == 'endpoint':
        original = application.service.dashboard
        def delayed_dashboard():
            result = original()
            pause()
            return result
        monkeypatch.setattr(application.service, 'dashboard', delayed_dashboard)
    else:
        original = application._set_sso_session
        def delayed_cookie(response, principal):
            if not entered.is_set():
                pause()
            return original(response, principal)
        monkeypatch.setattr(application, '_set_sso_session', delayed_cookie)

    with TestClient(application.app, base_url=BASE) as background, ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(background.get, '/api/dashboard', headers={'Cookie': f'{name}={old_cookie}'})
        assert entered.wait(10), 'background request did not reach the expected stage'
        try:
            assert logout(client, identity).status_code == 303
            assert database.one('SELECT count(*) AS n FROM sso_module_session_revocations')['n'] == 1
        finally:
            release.set()
        late = future.result(timeout=10)
        assert late.status_code == 401
        assert 'Max-Age=0' in late.headers.get('set-cookie', '')
        assert replay(client, settings, old_cookie).status_code == 401
        assert client.cookies.get(name) is None
        assert '/access/sso/revoke/' not in authority.calls


def test_previously_dispatched_rotated_cookie_is_revoked_and_fresh_login_is_independent(boundary):
    client, authority, settings, database = boundary
    identity = login(client, authority)
    name, _ = sso.cookie_names(settings)
    old = client.cookies.get(name)
    old_claims = sso.session_claims(settings, old)
    assert client.get('/api/dashboard').status_code == 200
    rotated = client.cookies.get(name)
    assert old != rotated
    assert sso.module_session_key(settings, sso.session_claims(settings, rotated)) == sso.module_session_key(settings, old_claims)
    assert logout(client, identity).status_code == 303
    # Even a Set-Cookie emitted before logout but delivered after it cannot resurrect authority.
    before = len(authority.calls)
    assert replay(client, settings, old).status_code == 401
    assert replay(client, settings, rotated).status_code == 401
    assert len(authority.calls) == before  # revoked before introspection
    new_identity = login(client, authority)
    assert new_identity['sub'] == identity['sub'] and new_identity['csrf'] != identity['csrf']
    assert client.get('/api/auth/session').status_code == 200
    assert database.one('SELECT count(*) AS n FROM sso_module_session_revocations')['n'] == 1


def test_other_person_and_other_login_of_same_person_are_not_revoked(boundary):
    client, authority, settings, _ = boundary
    identity = login(client, authority)
    with TestClient(application.app, base_url=BASE) as second, TestClient(application.app, base_url=BASE) as third:
        assert login(second, authority)['sub'] == ALICE
        assert login(third, authority, BOB)['sub'] == BOB
        assert logout(client, identity).status_code == 303
        assert second.get('/api/auth/session').status_code == 200
        assert third.get('/api/auth/session').status_code == 200


def test_revocation_survives_new_database_object_and_initialize(boundary, monkeypatch):
    client, authority, settings, database = boundary
    identity = login(client, authority)
    old = client.cookies.get(sso.cookie_names(settings)[0])
    assert logout(client, identity).status_code == 303
    persisted = database.all('SELECT * FROM sso_module_session_revocations')
    assert len(persisted) == 1
    assert set(persisted[0]) == {'session_hash', 'expires_at', 'revoked_at'}
    assert identity['csrf'] not in str(persisted) and old not in str(persisted)
    restarted = Database(database.path)
    restarted.initialize()
    monkeypatch.setattr(application, 'db', restarted)
    assert restarted.all('SELECT * FROM sso_module_session_revocations') == persisted
    assert replay(client, settings, old).status_code == 401


def test_revocation_hash_is_bound_to_subject_nonce_and_module_scope(boundary):
    _, _, settings, _ = boundary
    principal = {'sub': ALICE, 'csrf': 'x' * 43}
    key = sso.module_session_key(settings, principal)
    assert len(key) == 64
    assert key != sso.module_session_key(settings, {**principal, 'sub': BOB})
    assert key != sso.module_session_key(settings, {**principal, 'csrf': 'y' * 43})
    assert key != sso.module_session_key(replace(settings, sso_redirect_uri='https://procurement.test:19206/auth/sso/callback'), principal)


def test_additive_revocation_schema_upgrade_preserves_business_data(boundary):
    _, _, _, database = boundary
    before = database.all('SELECT * FROM templates ORDER BY code')
    with database.connection() as conn:
        conn.execute('DROP TABLE sso_module_session_revocations')  # only the synthetic fixture, emulate pre-fix schema
    database.initialize()
    assert database.all('SELECT * FROM templates ORDER BY code') == before
    assert database.all('SELECT * FROM sso_module_session_revocations') == []


def test_session_absolute_expiry_survives_refresh_and_cleanup_cannot_reactivate_cookie(boundary, monkeypatch):
    client, authority, settings, database = boundary
    identity = login(client, authority)
    name, _ = sso.cookie_names(settings)
    cookie = client.cookies.get(name)
    claims = sso.session_claims(settings, cookie)
    now = int(time.time())
    monkeypatch.setattr(time, 'time', lambda: now + 30)
    assert client.get('/api/dashboard').status_code == 200
    refreshed = sso.session_claims(settings, client.cookies.get(name))
    assert refreshed['session_exp'] == claims['session_exp']
    assert logout(client, identity).status_code == 303
    assert database.cleanup_sso_revocations(now=claims['session_exp'] - 1) == {'dry_run': True, 'eligible': 0, 'deleted': 0}
    assert database.cleanup_sso_revocations(now=claims['session_exp']) == {'dry_run': True, 'eligible': 1, 'deleted': 0}
    assert len(database.all('SELECT * FROM sso_module_session_revocations')) == 1
    assert database.cleanup_sso_revocations(now=claims['session_exp'], dry_run=False)['deleted'] == 1
    monkeypatch.setattr(time, 'time', lambda: claims['session_exp'] + 1)
    assert replay(client, settings, cookie).status_code == 401


def test_legacy_cookie_without_absolute_expiry_fails_closed(boundary):
    client, authority, settings, _ = boundary
    login(client, authority)
    cookie = client.cookies.get(sso.cookie_names(settings)[0])
    payload = sso.unseal(settings, 'session', cookie)
    payload.pop('session_exp')
    assert replay(client, settings, sso.seal(settings, 'session', payload)).status_code == 401


def test_cookie_expiry_is_clamped_to_session_deadline(boundary, monkeypatch):
    _, authority, settings, _ = boundary
    now = int(time.time())
    principal = sso.introspect(settings, authority.issue(ALICE))
    principal.update(csrf='x' * 43, session_exp=now + 10)
    assert sso.unseal(settings, 'session', sso.session_cookie(settings, principal))['exp'] <= now + 10
    monkeypatch.setattr(time, 'time', lambda: now + 11)
    with pytest.raises(sso.SSOError) as error:
        sso.session_cookie(settings, principal)
    assert error.value.status == 401


@pytest.mark.parametrize('operation', ['lookup', 'revoke'])
def test_storage_failure_is_fail_closed_not_fake_logout_success(boundary, monkeypatch, operation):
    client, authority, _, database = boundary
    identity = login(client, authority)
    def unavailable(*_):
        raise sqlite3.OperationalError('synthetic unavailable storage')
    if operation == 'lookup':
        monkeypatch.setattr(database, 'sso_session_revoked', unavailable)
        response = client.get('/api/dashboard')
    else:
        monkeypatch.setattr(database, 'revoke_sso_session', unavailable)
        response = logout(client, identity)
    assert response.status_code == 503 and 'synthetic unavailable' not in response.text
    assert 'set-cookie' not in response.headers


def test_cleanup_is_bounded_and_preserves_unexpired_tombstones(boundary):
    _, _, _, database = boundary
    for number in range(3):
        database.revoke_sso_session(str(number), 100, ALICE, 10)
    database.revoke_sso_session('still-active', 200, ALICE, 10)
    assert database.cleanup_sso_revocations(now=100, limit=2, dry_run=False)['deleted'] == 2
    assert database.sso_session_revoked('still-active')
    assert len(database.all('SELECT * FROM sso_module_session_revocations')) == 2
    with pytest.raises(ValueError):
        database.cleanup_sso_revocations(now=100, limit=1001, dry_run=False)


def test_repeated_server_revocation_is_idempotent(boundary):
    _, _, _, database = boundary
    database.revoke_sso_session('test-hash', 100, ALICE, 10)
    database.revoke_sso_session('test-hash', 200, ALICE, 20)
    row = database.one("SELECT * FROM sso_module_session_revocations WHERE session_hash='test-hash'")
    assert row['expires_at'] == 100 and row['revoked_at'] == 10
    assert database.one("SELECT count(*) AS n FROM audit_log WHERE action='sso_logout'")['n'] == 1
