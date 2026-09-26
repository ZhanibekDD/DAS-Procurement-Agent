"""The real application routes and identity middleware on isolated test DBs."""
import json
from dataclasses import replace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import procurement.app as application
from procurement.launch_routes import install
from procurement.launch_workflow import LaunchWorkflow
from procurement.db import Database
from procurement.config import Settings
from procurement import sso
from procurement.service import ProcurementService
from test_sso_adapter import Authority, ALICE, BOB, BASE, DAS, login, headers
from test_launch_workflow import FIXTURES, lot_payload


@pytest.fixture
def http_boundary(tmp_path,monkeypatch):
    settings=Settings(environment='canary',api_key='unused-legacy-key',db_path=str(tmp_path/'http.db'),
        outbox_mode='draft_only',auth_secret='test-state-'+'x'*40,admin_username='',admin_password_hash='',session_ttl_seconds=3600,
        sso_enabled=True,sso_authorize_url=DAS+'/access/sso/authorize/',sso_internal_base_url='http://das-identity.test:8000',
        sso_client_secret='test-service-'+'x'*40,sso_redirect_uri=BASE+'/auth/sso/callback')
    db=Database(settings.db_path);db.initialize();service=ProcurementService(db);launch=LaunchWorkflow(service)
    for name,value in [('settings',settings),('db',db),('service',service)]:monkeypatch.setattr(application,name,value)
    authority=Authority();monkeypatch.setattr(sso,'_post',authority.post)
    app=FastAPI();app.middleware('http')(application.das_identity_boundary)
    app.router.routes.extend(r for r in application.app.router.routes if not r.path.startswith('/api/launch/') and r.path!='/assets/launch.js')
    install(app,settings,service,launch,application.require_access,application._session_claims,application.handle_domain_error)
    with TestClient(app,base_url=BASE) as client:yield client,authority,settings,db


def test_auth_csrf_read_only_download_get_head_range_and_actor(http_boundary):
    client,authority,settings,db=http_boundary
    assert client.get('/api/launch/suppliers').status_code==401
    alice=login(client,authority)
    pr=client.post('/api/projects',headers=headers(alice),json=dict(name='ТЕСТ',region='Воронеж',delivery_address='Тестовая 1')).json()
    data=(FIXTURES/'items.xlsx').read_bytes()
    response=client.post('/api/documents',headers=headers(alice),params={'document_type':'project_section','project_id':pr['id']},files={'file':('items.xlsx',data)})
    assert response.status_code==201,response.text
    doc=response.json();url=f"/api/launch/documents/{doc['id']}/download"
    assert client.get(url).content==data
    assert client.head(url).status_code==200
    response=client.get(url,headers={'Range':'bytes=0-9'})
    assert response.status_code==206 and response.content==data[:10]
    assert client.post('/api/launch/supplier-import/preview',files={'file':('suppliers.csv',(FIXTURES/'suppliers.csv').read_bytes())}).status_code==403
    p=client.post('/api/launch/supplier-import/preview',headers=headers(alice),files={'file':('suppliers.csv',(FIXTURES/'suppliers.csv').read_bytes())}).json()
    assert p['report']['added']==2
    bob=login(client,authority,BOB)
    assert client.post(f"/api/launch/supplier-import/{p['preview_id']}/apply",headers=headers(bob),json={'confirmed':True}).status_code==404
    authority.users[BOB]['read_only']=True
    bob=client.get('/api/auth/session').json()
    assert client.get(url,headers={'Range':'bytes=0-9'}).status_code==206
    assert client.post('/api/launch/supplier-import/preview',headers=headers(bob),files={'file':('suppliers.csv',(FIXTURES/'suppliers.csv').read_bytes())}).status_code==403
    authority.users[BOB]['modules']=[]
    for method in ('GET','HEAD'):
        assert client.request(method,url,headers={'Range':'bytes=0-9'}).status_code==403
    client.cookies.clear()
    assert client.get(url,headers={'Range':'bytes=0-9','X-OpenWebUI-User-Id':ALICE}).status_code==401
    assert not db.all('SELECT * FROM suppliers')
    assert {r['actor'] for r in db.all("SELECT * FROM audit_log WHERE action='previewed'")}=={ALICE}


def test_http_mapping_confirmation_sheet_review_and_source_validation(http_boundary):
    client,authority,settings,db=http_boundary
    alice=login(client,authority);h=headers(alice)
    p=client.post('/api/launch/supplier-import/preview',headers=h,files={'file':('suppliers.xlsx',(FIXTURES/'suppliers.xlsx').read_bytes())}).json()
    apply=f"/api/launch/supplier-import/{p['preview_id']}/apply"
    assert client.post(apply,headers=h,json={'confirmed':'true'}).status_code==422
    assert client.post(apply,headers=h,json={'confirmed':False}).status_code==422
    assert client.post(apply,headers=h,json={'confirmed':True}).json()['added']==2
    pr=client.post('/api/projects',headers=h,json=dict(name='ТЕСТ',region='Воронеж',delivery_address='Тестовая 1')).json()
    preview=client.post('/api/launch/lot-sheet/preview',headers=h,data={'project_id':pr['id']},files={'file':('items.xlsx',(FIXTURES/'items.xlsx').read_bytes())}).json()
    payload=lot_payload(pr['id']);payload['items'][0]['quantity']='12.5'
    create=f"/api/launch/lot-sheet/{preview['preview_id']}/create"
    response=client.post(create,headers=h,json={'confirmed':True,'lot':payload})
    assert response.status_code==201,response.text
    lot=response.json();assert lot['items'][0]['quantity']=='12.5' and len(lot['attachments'])==1
    assert client.post(create,headers=h,json={'confirmed':True,'lot':payload}).json()['id']==lot['id']
    assert db.one('SELECT count(*) n FROM lots')['n']==1
    assert client.post('/api/launch/supplier-import/preview',headers=h,files={'file':('../a.xlsx',b'PKfake')}).status_code==422
    assert client.post('/api/launch/supplier-import/preview',headers=h,data={'mapping':json.dumps({'name':0,'email':0})},files={'file':('suppliers.csv',(FIXTURES/'suppliers.csv').read_bytes())}).json()['report']['error']==4
