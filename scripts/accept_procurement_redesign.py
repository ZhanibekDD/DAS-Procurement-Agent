"""Live canary gate. Never submits email to a real SMTP server."""
import hashlib
import re
from pathlib import Path
from urllib.parse import urlsplit

import httpx


def accept(url,credentials,capture,reference):
    assert urlsplit(url).hostname in {'localhost','127.0.0.1'} and capture is not None
    checks=[]
    def check(name,ok):
        if not ok:raise AssertionError(name)
        checks.append(name)
    with httpx.Client(base_url=url,timeout=120) as c:
        check('personal login',c.post('/auth/login',data=credentials).status_code==303)
        root=c.get('/').text
        c.headers['X-Launch-CSRF-Token']=re.search(r'<meta name="procurement-launch-csrf" content="([a-f0-9]+)">',root)[1]
        def request(method,path,status=200,**kw):
            r=c.request(method,path,**kw);check(method+' '+path,r.status_code==status)
            return r.json()
        pr=request('POST','/api/projects',201,json={'name':'ТЕСТ Проект 121-25','region':'Воронежская область','delivery_address':'Тестовый адрес'})
        raw=Path(reference).read_bytes();digest=hashlib.sha256(raw).hexdigest()
        workbook=f"/api/procurement/projects/{pr['id']}/workbook"
        p=request('POST',workbook,files={'file':('Расчет_материалов_121-25.xlsx',raw)})
        check('four exact reference sheets',{r['sheet'] for r in p['rows']}=={'Итоги','Исходные','Спецификации','Источники'})
        check('four reference statuses',{r['status'] for r in p['rows']}>={'по ведомости','по проекту','предварительно','не определено'})
        request('POST',workbook,409,data={'confirmed':'true','expected_sha256':'0'*64},files={'file':('Расчет_материалов_121-25.xlsx',raw)})
        doc=request('POST',workbook,data={'confirmed':'true','expected_sha256':p['sha256']},files={'file':('Расчет_материалов_121-25.xlsx',raw)})
        did=doc['document_id'];download=f'/api/launch/documents/{did}/download'
        check('reference original exact SHA',hashlib.sha256(c.get(download).content).hexdigest()==digest)
        for method in ('GET','HEAD'):
            check('owner original '+method,c.request(method,download).status_code==200)
            check('anonymous original '+method,httpx.request(method,url+download).status_code==403)
        check('owner original Range',c.get(download,headers={'Range':'bytes=0-15'}).content==raw[:16])
        check('forged user Range denied',httpx.get(url+download,headers={'Range':'bytes=0-15','X-OpenWebUI-User-Id':'admin'}).status_code==403)
        for sheet in ('Итоги','Исходные','Спецификации','Источники'):
            view=c.get(f'/api/procurement/documents/{did}/view',params={'sheet':sheet})
            check('real XLSX viewer '+sheet,view.status_code==200 and '<table>' in view.text)
        portfolio=request('GET',f"/api/procurement/projects/{pr['id']}")
        check('source rows and document SHA',len(portfolio['workbook_rows'])==len(p['rows']) and all(r['sha256']==digest for r in portfolio['workbook_rows']))
        fbs=[('ФБС 24.4.6','218'),('ФБС 12.4.6','95'),('ФБС 9.4.6','128')]
        check('real reference FBS exact quantities',all(any(m['name']==name and m['quantity']==qty for m in portfolio['materials']) for name,qty in fbs))
        lot=request('POST','/api/lots',201,json={'project_id':pr['id'],'title':'Блоки ФБС','region':pr['region'],'delivery_address':pr['delivery_address'],
            'response_deadline':'2026-10-31','currency':'RUB','attachment_document_ids':[did],'items':[{'name':n,'quantity':q,'unit':'шт'} for n,q in fbs]})
        pb=request('POST','/api/lots',201,json={'project_id':pr['id'],'title':'Плиты ПБ','region':pr['region'],'delivery_address':pr['delivery_address'],
            'response_deadline':'2026-10-31','currency':'RUB','items':[{'name':'ПБ 63.12','quantity':'30','unit':'шт'}]})
        supplier=request('POST','/api/suppliers',201,json={'name':'ТЕСТ ФБС canary','email':'fbs@example.test','region':'Воронежская область'})
        data={'supplier_ids':[supplier['id']],'item_ids':[r['id'] for r in lot['items']]}
        preview=f"/api/procurement/lots/{lot['id']}/preview"
        request('POST',preview,409,json={**data,'item_ids':[pb['items'][0]['id']]})
        p=request('POST',preview,json=data)
        check('FBS preview contains no PB','ПБ' not in p['messages'][0]['body'] and all(f'{n}: {q} шт' in p['messages'][0]['body'] for n,q in fbs))
        check('no separate default approval',not p['approval_required'])
        campaign=request('POST',f"/api/lots/{lot['id']}/campaigns",201,json={**data,'snapshot_sha256':p['snapshot_sha256'],'preview_sha256':p['preview_sha256']})
        mid=campaign['messages'][0]['id'];send=f'/api/procurement/outbox/{mid}/send'
        request('POST',send,409,json={'lot_id':pb['id'],'snapshot_sha256':p['snapshot_sha256'],'confirmed':True})
        before=len(capture.messages)
        r=request('POST',send,json={'lot_id':lot['id'],'snapshot_sha256':p['snapshot_sha256'],'confirmed':True})
        check('inline SMTP confirmed once',r['status']=='sent' and len(capture.messages)==before+1)
        attached=list(capture.messages[-1].iter_attachments())
        check('real RFQ attachment original exact SHA',len(attached)==1 and hashlib.sha256(attached[0].get_payload(decode=True)).hexdigest()==digest)
        request('POST',send,json={'lot_id':lot['id'],'snapshot_sha256':p['snapshot_sha256'],'confirmed':True})
        check('no duplicate SMTP',len(capture.messages)==before+1)
        request('POST','/api/procurement/catalog/preview',422,files={'file':('bad.exe',b'bad')})
        from procurement.catalog import ALIASES
        from email.message import EmailMessage
        keys=list(ALIASES)
        def csv(price='112',email='a@example.test',name='ТЕСТ Прайс A',date='2020-01-01'):
            row=dict(item_name='ФБС 24.4.6',specification='B7.5',category='ФБС',unit='шт',unit_price=price,currency='RUB',vat='с НДС',delivery='доставка включена',region='Воронежская область',minimum_batch='10',price_date=date,valid_until='2099-01-01',supplier_name=name,email=email)
            return (';'.join(keys)+'\n'+';'.join(row.get(k,'') for k in keys)+'\n').encode()
        for price,email,name,date in [('112','a@example.test','ТЕСТ Прайс A','2020-01-01'),('88','b@example.test','ТЕСТ Прайс B','2020-01-01'),('125','a@example.test','ТЕСТ Прайс A','2020-01-02')]:
            preview=request('POST','/api/procurement/catalog/preview',files={'file':('price.csv',csv(price,email,name,date))})
            check('all financial fields validated',len(preview['rows'])==1 and not preview['errors'])
            applied=request('POST',f"/api/procurement/catalog/{preview['preview_id']}/apply",json={'confirmed':True})
            check('append one immutable price',applied['added']==1)
            check('idempotent price reapply',request('POST',f"/api/procurement/catalog/{preview['preview_id']}/apply",json={'confirmed':True})==applied)
        rows=request('GET','/api/procurement/catalog?q=ФБС&specification=B7.5')
        check('three retained price history rows',len(rows)==3 and sum(r['current'] for r in rows)==2)
        check('price index independent of rating',all(r['reliability']==3 for r in rows) and any(r['price_index_pct'] is not None for r in rows))
        mail=EmailMessage();mail['From']='untrusted@example.test';mail['To']='test@example.test';mail.set_content('Прайс')
        mail.add_attachment(csv('90','c@example.test','ТЕСТ Прайс C'),maintype='text',subtype='csv',filename='mail-price.csv')
        previews=request('POST','/api/procurement/catalog/incoming-mail',files={'file':('price.eml',mail.as_bytes())})
        check('incoming attachment preview not implicit import',len(previews['previews'])==1 and len(request('GET','/api/procurement/catalog'))==3)
        ep=previews['previews'][0]
        request('POST',f"/api/procurement/catalog/{ep['preview_id']}/apply",json={'confirmed':True})
        # Real Office files are parsed/opened independently, not accepted by suffix alone.
        import io
        import zipfile
        from PIL import Image
        from pypdf import PdfReader
        image=io.BytesIO();Image.new('RGB',(20,20),'white').save(image,'PNG')
        word=io.BytesIO()
        with zipfile.ZipFile(word,'w') as z:
            z.writestr('[Content_Types].xml','<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
            z.writestr('_rels/.rels','<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
            z.writestr('word/document.xml','<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>001230040500 — 0 — 10</w:t></w:r></w:p></w:body></w:document>')
        pdf=Path(__file__).parents[1]/'tests/fixtures/russian_scan.pdf'
        for name,data,marker in [('original.png',image.getvalue(),None),('original.docx',word.getvalue(),'001230040500'),('original.pdf',pdf.read_bytes(),None)]:
            d=request('POST','/api/documents',201,params={'document_type':'project_section','project_id':pr['id']},files={'file':(name,data)})
            view=c.get(f"/api/procurement/documents/{d['id']}/view")
            check('inline actual '+name,view.status_code==200 and (marker in view.text if marker else view.content==data))
            original=c.get(f"/api/launch/documents/{d['id']}/download").content
            check('original SHA '+name,hashlib.sha256(original).hexdigest()==hashlib.sha256(data).hexdigest())
            if name.endswith('.pdf'):check('PDF opens independently',len(PdfReader(io.BytesIO(original)).pages)>0)
            if name.endswith('.png'):
                with Image.open(io.BytesIO(original)) as img:img.verify()
                check('image opens independently',True)
            if name.endswith('.docx'):
                with zipfile.ZipFile(io.BytesIO(original)) as z:check('DOCX opens independently',marker in z.read('word/document.xml').decode())
        request('PUT',f"/api/procurement/projects/{pr['id']}/budget",json={'currency':'RUB','amount':'1234500.00'})
        check('explicit project budget retained',request('GET',f"/api/procurement/projects/{pr['id']}")['budget'][0]['amount']=='1234500.00')
        check('service health',c.get('/health').status_code==200)
    return checks
