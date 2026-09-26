"""Real project PDF and 100 MiB HTTP boundary; isolated loopback canary only."""
import hashlib
import io
import re
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from pypdf import PdfReader
from procurement.upload_io import MAX_FILE,TOO_LARGE,FilePayload,payload_sha256


def accept(url,credentials,capture,pdf_path):
    assert urlsplit(url).hostname in {'127.0.0.1','localhost'} and capture is not None
    checks=[]
    def check(name,condition):
        if not condition:raise AssertionError(name)
        checks.append(name)
    with httpx.Client(base_url=url,timeout=120) as client:
        check('personal login',client.post('/auth/login',data=credentials).status_code==303)
        html=client.get('/').text
        csrf=re.search(r'<meta name="procurement-csrf" content="([a-f0-9]+)">',html)[1]
        launch_csrf=re.search(r'<meta name="procurement-launch-csrf" content="([a-f0-9]+)">',html)[1]
        client.headers.update({'X-CSRF-Token':csrf,'X-Launch-CSRF-Token':launch_csrf})
        def post(path,expected=201,**kw):
            r=client.post(path,**kw)
            check('POST '+path,r.status_code==expected)
            return r.json()
        pr=post('/api/projects',json={'name':'ТЕСТ 413 реальный проект PDF','region':'Свердловская область','delivery_address':'Тестовый адрес'})
        with open(pdf_path,'rb') as source:
            doc=post('/api/documents',params={'document_type':'project_section','project_id':pr['id']},files={'file':('project_page13.pdf',source,'application/pdf')})
        check('real PDF size over old proxy',doc['size_bytes']>1024*1024)
        expected_sha=payload_sha256(FilePayload(Path(pdf_path)))
        check('source exact hash',doc['sha256']==expected_sha)
        download=f"/api/launch/documents/{doc['id']}/download"
        r=client.get(download)
        check('PDF downloaded and SHA',r.status_code==200 and hashlib.sha256(r.content).hexdigest()==expected_sha)
        reader=PdfReader(io.BytesIO(r.content))
        check('real PDF opens, 22 pages',len(reader.pages)==22)
        extracted=post(f"/api/documents/{doc['id']}/extract/fence-schedule?page=13")['suggestion']
        check('six positions preview',len(extracted['items'])==6 and [x['quantity'] for x in extracted['items']]==['9','12','6','28','1','1'])
        check('no lot before confirmation',not any(x['project_id']==pr['id'] for x in client.get('/api/lots').json()))
        result=post(f"/api/procurement-suggestions/{extracted['id']}/approve",json={'approved_by':'Canary test',
            'response_deadline':'2026-10-31','rfq_requirements':{'delivery_address_confirmation':'Тестовый адрес',
                'coating':'цинк','color_ral':'RAL 6005','mesh_cell':'50x200','rod_diameter':'5 мм','delivery_or_pickup':'supplier_choice'}})
        lot=result['lot']
        check('source attached after confirmation',len(lot['attachments'])==1 and lot['attachments'][0]['sha256']==expected_sha)
        supplier=post('/api/suppliers',json={'name':'ТЕСТ только SMTP fixture','region':pr['region'],'email':'test@example.test'})
        message=post(f"/api/lots/{lot['id']}/campaigns",json={'supplier_ids':[supplier['id']]})['messages'][0]
        check('RFQ source snapshot exact',message['attachments'][0]['sha256']==expected_sha)
        post(f"/api/outbox/{message['id']}/approve",200,json={'approved_by':'Canary test'})
        before=len(capture.messages)
        sent=post(f"/api/launch/outbox/{message['id']}/send",200,json={'confirmed':True})
        check('real SMTP accepted, no false-ready',sent['accepted_by_smtp'] and len(capture.messages)==before+1)
        attachment=list(capture.messages[-1].iter_attachments())[0]
        check('actual MIME PDF filename and SHA',attachment.get_filename()=='project_page13.pdf' and
            hashlib.sha256(attachment.get_payload(decode=True)).hexdigest()==expected_sha)
        for method in ['GET','HEAD']:
            check('owner '+method,client.request(method,download).status_code==200)
            check('anonymous '+method,httpx.request(method,url+download).status_code==403)
        check('owner Range',client.get(download,headers={'Range':'bytes=0-9'}).status_code==206)
        check('spoofed Range denied',httpx.get(url+download,headers={'Range':'bytes=0-9','X-OpenWebUI-User-Id':'admin'}).status_code==403)
        # Disk-backed boundary fixtures, not valid PDFs for extraction. No mail send.
        with tempfile.TemporaryFile('w+b') as large:
            large.write(b'%PDF-1.4\n');large.truncate(MAX_FILE);large.seek(0)
            limit=post('/api/documents',params={'document_type':'project_section','project_id':pr['id']},files={'file':('boundary.pdf',large)})
            check('100 MiB exact accepted',limit['size_bytes']==MAX_FILE)
            large.seek(0,2);large.write(b'x');large.seek(0)
            r=client.post('/api/documents',params={'document_type':'project_section','project_id':pr['id']},files={'file':('over.pdf',large)})
            check('max+1 rejected friendly',r.status_code==413 and r.json()['detail']==TOO_LARGE)
        check('API health',client.get('/health').status_code==200)
    return checks
