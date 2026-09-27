"""Loopback-only real OCR/CRUD/MIME gate. Refuses Production mail sending."""
import hashlib,io,re
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from pypdf import PdfReader


def accept(url, credentials, capture, real_scan):
    assert urlsplit(url).hostname in {'127.0.0.1','localhost'} and capture is not None
    checks=[]
    def check(name,ok):
        if not ok:raise AssertionError(name)
        checks.append(name)
    with httpx.Client(base_url=url,timeout=120) as client:
        def login():
            check('personal login',client.post('/auth/login',data=credentials).status_code==303)
            root=client.get('/').text
            client.headers['X-Launch-CSRF-Token']=re.search(r'<meta name="procurement-launch-csrf" content="([a-f0-9]+)">',root)[1]
        login()
        def request(method,path,expected=200,**kwargs):
            response=client.request(method,path,**kwargs)
            check(method+' '+path,response.status_code==expected)
            return response.json()
        supplier=request('POST','/api/suppliers',201,json={'name':'ТЕСТ OCR canary CRUD','region':'Воронежская область',
                   'phone':'+7 (473) 200–00–01','email':'ocr@example.test'})
        sid=supplier['id'];card=request('GET',f'/api/launch/suppliers/{sid}')
        check('create persists',card['phone']=='+74732000001')
        values={k:card[k] for k in ('name','tax_id','region','phone','email','telegram','max_contact','cluster','categories','rating','verified')}
        values.update(name='ТЕСТ OCR edited',telegram='@edited',rating=4.5)
        edited=request('PUT',f'/api/launch/suppliers/{sid}',json={**values,'revision':card['revision']})
        login();read=request('GET',f'/api/launch/suppliers/{sid}')
        check('edit persists relogin',read['name']==values['name'] and read['telegram']=='@edited')
        request('DELETE',f'/api/launch/suppliers/{sid}',422,json={'confirmed':False,'revision':edited['revision']})
        deleted=request('DELETE',f'/api/launch/suppliers/{sid}',json={'confirmed':True,'revision':edited['revision']})
        check('soft delete',not any(r['id']==sid for r in request('GET','/api/suppliers')))
        check('delete audited',any(r['action']=='supplier_soft_deleted' and str(r['entity_id'])==str(sid) for r in request('GET','/api/audit?limit=50')))
        request('POST',f'/api/launch/suppliers/{sid}/restore',json={'confirmed':True,'revision':deleted['revision']})
        project=request('POST','/api/projects',201,json={'name':'ТЕСТ OCR скан проекта','region':'Воронежская область','delivery_address':'Тестовый адрес'})
        scan_path=Path(real_scan);digest=hashlib.sha256(scan_path.read_bytes()).hexdigest()
        check('actual uploaded scan has no text layer',len(PdfReader(scan_path).pages)==4 and all(not p.extract_text() for p in PdfReader(scan_path).pages))
        with scan_path.open('rb') as stream:
            doc=request('POST','/api/documents',201,params={'document_type':'project_section','project_id':project['id']},files={'file':('ФБС.pdf',stream,'application/pdf')})
        check('scan original SHA',doc['sha256']==digest)
        preview=request('POST',f"/api/documents/{doc['id']}/extract/fence-schedule?page=2",201)
        check('real Russian OCR reviewed',preview['review_kind']=='pdf_ocr' and preview['decision']=='human_review_required' and preview['preview']['lines'])
        check('scan quantities not silently guessed',all(r['quantity']=='' and r['unit']=='' for r in preview['preview']['rows']))
        check('no automatic lot',not any(r['project_id']==project['id'] for r in request('GET','/api/lots')))
        # Independently read quantities from the original specification on PDF page 2.
        items=[{'name':name,'quantity':qty,'unit':'шт.','specification':'Ручная сверка с таблицей исходного PDF'}
               for name,qty in [('ФБС 24.4.6','218'),('ФБС 12.4.6','95'),('ФБС 9.4.6','128')]]
        lot={'project_id':project['id'],'title':'ТЕСТ ФБС после ручной проверки','region':project['region'],
             'delivery_address':project['delivery_address'],'response_deadline':'2026-10-31','currency':'RUB','items':items}
        create=f"/api/launch/pdf-review/{preview['preview']['preview_id']}/create"
        request('POST',create,422,json={'confirmed':True,'reviewed_line_ids':[1],'lot':lot})
        lot=request('POST',create,201,json={'confirmed':True,'reviewed_line_ids':[r['line'] for r in preview['preview']['lines']],'lot':lot})
        check('corrected FBS quantities',[r['quantity'] for r in lot['items']]==['218','95','128'])
        check('original attached without modification',lot['attachments'][0]['sha256']==digest)
        download=f"/api/launch/documents/{doc['id']}/download"
        data=client.get(download).content
        check('download real PDF opens and original SHA',len(PdfReader(io.BytesIO(data)).pages)==4 and hashlib.sha256(data).hexdigest()==digest)
        for method in ('GET','HEAD'):
            check('owner '+method,client.request(method,download).status_code==200)
            check('anonymous '+method,httpx.request(method,url+download).status_code==403)
        check('owner Range',client.get(download,headers={'Range':'bytes=0-9'}).status_code==206)
        check('forged Range denied',httpx.get(url+download,headers={'Range':'bytes=0-9','X-OpenWebUI-User-Id':'admin'}).status_code==403)
        message=request('POST',f"/api/lots/{lot['id']}/campaigns",201,json={'supplier_ids':[sid]})['messages'][0]
        check('RFQ source exact SHA',message['attachments'][0]['sha256']==digest)
        request('POST',f"/api/outbox/{message['id']}/approve",json={'approved_by':'Canary OCR'})
        count=len(capture.messages)
        sent=request('POST',f"/api/launch/outbox/{message['id']}/send",json={'confirmed':True})
        check('SMTP fixture received original',sent['accepted_by_smtp'] and len(capture.messages)==count+1)
        attached=list(capture.messages[-1].iter_attachments())[0]
        check('actual MIME original scan unchanged',attached.get_filename()=='ФБС.pdf' and hashlib.sha256(attached.get_payload(decode=True)).hexdigest()==digest)
        check('service healthy',client.get('/health').status_code==200)
    return checks
