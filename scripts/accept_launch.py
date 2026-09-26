"""Real HTTP + MIME acceptance on an isolated, loopback-only canary.

Refuses public URLs. Captured SMTP messages must be supplied by the fixture.
Never sends an RFQ to a real mailbox and never logs auth/session values.
"""
import hashlib
import io
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from openpyxl import load_workbook


def accept(url,credentials,capture):
    assert urlsplit(url).hostname in {'127.0.0.1','localhost'}
    assert capture is not None
    fixtures=Path(__file__).resolve().parents[1]/'tests'/'fixtures'
    client=httpx.Client(base_url=url,timeout=35)
    anonymous=httpx.Client(base_url=url,timeout=35)
    checks=[]
    def check(name,condition):
        if not condition:raise AssertionError(name)
        checks.append(name)
    def request(method,path,expected=200,**kwargs):
        r=client.request(method,path,**kwargs)
        check(method+' '+path.split('?')[0],r.status_code==expected)
        return r.json()
    try:
        check('anonymous denied',anonymous.get('/api/launch/suppliers').status_code==403)
        login=client.post('/auth/login',data=credentials,follow_redirects=False)
        check('real login',login.status_code==303)
        import re
        root=client.get('/').text
        match=re.search(r'<meta name="procurement-launch-csrf" content="([a-f0-9]+)">',root)
        check('CSRF supplied',bool(match))
        client.headers['X-Launch-CSRF-Token']=match[1]
        p=request('POST','/api/launch/supplier-import/preview',files={'file':('suppliers.xlsx',(fixtures/'suppliers.xlsx').read_bytes())})
        check('XLSX preview report',p['report']==dict(added=2,updated=0,skipped=1,error=1))
        apply=f"/api/launch/supplier-import/{p['preview_id']}/apply"
        result=request('POST',apply,json={'confirmed':True})
        check('idempotent import',request('POST',apply,json={'confirmed':True})==result)
        request('POST',f"/api/launch/supplier-import/{p['preview_id']}/rollback",json={'confirmed':True})
        deleted=request('GET','/api/launch/suppliers')
        for s in deleted:
            request('POST',f"/api/launch/suppliers/{s['id']}/restore",json={'confirmed':True,'revision':s['revision']})
        csv=request('POST','/api/launch/supplier-import/preview',files={'file':('suppliers.csv',(fixtures/'suppliers.csv').read_bytes())})
        check('CSV INN dedupe',csv['report']==dict(added=0,updated=0,skipped=3,error=1))
        request('POST',f"/api/launch/supplier-import/{csv['preview_id']}/apply",json={'confirmed':True})
        suppliers=request('GET','/api/suppliers');sid=suppliers[0]['id']
        s=request('GET',f'/api/launch/suppliers/{sid}')
        edit={k:s[k] for k in ('name','tax_id','region','email','phone','telegram','max_contact','cluster','categories','rating','verified')}
        edit.update(name='ТЕСТ исправленный поставщик',email='edited@example.test',telegram='@edited',max_contact='edited-max',rating=4.5,verified=True)
        s=request('PUT',f'/api/launch/suppliers/{sid}',json={**edit,'revision':s['revision']})
        check('all editable fields',all(s[k]==v for k,v in edit.items()))
        deleted=request('DELETE',f'/api/launch/suppliers/{sid}',json={'confirmed':True,'revision':s['revision']})
        check('soft deleted not listed',not any(x['id']==sid for x in request('GET','/api/suppliers')))
        request('POST',f'/api/launch/suppliers/{sid}/restore',json={'confirmed':True,'revision':deleted['revision']})
        project=request('POST','/api/projects',201,json={'name':'ТЕСТ проект','region':'Воронежская область','delivery_address':'Воронеж, тестовая 1'})
        source=(fixtures/'items.xlsx').read_bytes()
        sheet=request('POST','/api/launch/lot-sheet/preview',data={'project_id':project['id']},files={'file':('items.xlsx',source)})
        check('sheet qty date specs',sheet['rows'][0]['quantity']=='10' and sheet['rows'][1]['quantity']=='2.5' and len(sheet['errors'])==1)
        items=[{k:r.get(k) for k in ('name','quantity','unit','specification','delivery_date')} for r in sheet['rows'][:2]]
        items[0]['quantity']='12.5';items[0]['specification']='Проверено; код 001230040500; исполнение 0'
        payload=dict(project_id=project['id'],title='ТЕСТ заявка из листа',region=project['region'],delivery_address=project['delivery_address'],
            response_deadline='2026-10-15',currency='RUB',items=items)
        lot=request('POST',f"/api/launch/lot-sheet/{sheet['preview_id']}/create",201,json={'confirmed':True,'lot':payload})
        check('edited preview created',lot['items'][0]['quantity']=='12.5' and lot['items'][0]['specification']==items[0]['specification'])
        check('source automatically attached',len(lot['attachments'])==1)
        did=lot['attachments'][0]['document_id'];download=f'/api/launch/documents/{did}/download'
        file=client.get(download)
        check('real file download SHA',file.status_code==200 and hashlib.sha256(file.content).digest()==hashlib.sha256(source).digest())
        book=load_workbook(io.BytesIO(file.content),read_only=True);check('real XLSX open',book.active['A2'].value=='Кабель 001230040500');book.close()
        check('HEAD owner',client.head(download).status_code==200)
        file=client.get(download,headers={'Range':'bytes=0-9'})
        check('Range owner',file.status_code==206 and file.content==source[:10])
        check('anonymous Range denied',anonymous.get(download,headers={'Range':'bytes=0-9'}).status_code==403)
        check('spoofed user header denied',anonymous.get(download,headers={'X-OpenWebUI-User-Id':'admin'}).status_code==403)
        campaign=request('POST',f"/api/lots/{lot['id']}/campaigns",201,json={'supplier_ids':[sid]})
        mid=campaign['messages'][0]['id']
        check('RFQ snapshot attached',len(campaign['messages'][0]['attachments'])==1)
        request('POST',f'/api/outbox/{mid}/approve',json={'approved_by':'test-browser','comment':'Canary fixture approval'})
        before=len(capture.messages)
        request('POST',f'/api/launch/outbox/{mid}/send',json={'confirmed':True})
        check('SMTP real wire one message',len(capture.messages)==before+1)
        email=capture.messages[-1];attachments=list(email.iter_attachments())
        check('MIME exact attachment',len(attachments)==1 and attachments[0].get_filename()=='items.xlsx' and attachments[0].get_payload(decode=True)==source)
        body=email.get_body(preferencelist=('plain',)).get_content()
        check('MIME exact specs quantities deadlines','001230040500' in body and '12.5' in body and '2026-10-15' in body and 'исполнение 0' in body)
        check('duplicate send suppressed',request('POST',f'/api/launch/outbox/{mid}/send',json={'confirmed':True})['duplicate'] and len(capture.messages)==before+1)
        check('unsafe upload denied',client.post('/api/launch/supplier-import/preview',files={'file':('file.exe.xlsx',source)}).status_code==422)
        audit=request('GET','/api/audit?limit=200')
        check('audit actions recorded',{'supplier_edited','supplier_soft_deleted','supplier_restored','supplier_import_applied','supplier_import_rolled_back','lot_created_from_sheet','mail_sent'} <= {a['action'] for a in audit})
        check('health',client.get('/health').status_code==200)
        return checks
    finally:client.close();anonymous.close()


def main():
    raise SystemExit('Use the canary driver; public/prod write acceptance is prohibited by this fixture.')

if __name__=='__main__':main()
