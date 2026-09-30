"""Authenticated procurement workflow, catalog, portfolio and document views."""
import json
import re
from datetime import date, timedelta
from pathlib import Path
from fastapi import Depends,File,Form,HTTPException,Request,UploadFile
from fastapi.responses import HTMLResponse,FileResponse
from pydantic import Field
from starlette.concurrency import run_in_threadpool
from .models import CampaignCreate,LotCreate,StrictModel
from .catalog import Catalog,workbook_rows,number,ALIASES
from .procurement_flow import ProcurementFlow,validate_message
from .upload_io import staged_upload
from .table_ingest import read_table
from .db import utcnow
from .identity import trusted_actor
from .launch_routes import Confirm
from .price_memory import PriceMemory


class Send(StrictModel):
    lot_id:int=Field(gt=0)
    snapshot_sha256:str=Field(pattern=r'^[a-f0-9]{64}$')
    confirmed:bool=Field(strict=True)


class Decision(StrictModel):
    quote_id:int=Field(gt=0)
    stage:str=Field(pattern=r'^(awarded|ordered)$')


class Policy(StrictModel):
    amount_threshold:str|None=None
    currency:str=Field(default='RUB',pattern='^[A-Z]{3}$')
    required_roles:list[str]=Field(default_factory=list,max_length=2)


class Budget(StrictModel):
    amount:str
    currency:str=Field(pattern='^[A-Z]{3}$')


class ReviewedPriceRows(StrictModel):
    rows:list[dict[str,str]]=Field(min_length=1,max_length=500)
    confirmed_rub:bool=Field(default=False,strict=True)

class ReadAlerts(StrictModel):
    event_ids:list[str]=Field(min_length=1,max_length=100)

class QuickMapping(StrictModel):
    mapping:dict[str,int]


def install(app,settings,service,launch,require_access,write_access,session_claims,domain_error):
    flow=ProcurementFlow(service);catalog=Catalog(service,launch)
    memory=PriceMemory(service)
    def call(fn,*args):
        try:return fn(*args)
        except Exception as exc:raise domain_error(exc) from None

    from .incoming_routes import install as install_incoming
    install_incoming(app,service,launch,require_access,write_access,call)

    def role(request):
        principal=getattr(request.state,'das_principal',{}) or session_claims(request.cookies.get('procurement_session','')) or {}
        return principal.get('role','staff')

    def visible_document(document):
        return {key:document[key] for key in ('id','project_id','filename','sha256','size_bytes')}

    def quick_lot_title(filename):
        stem=Path(filename).stem.strip()[:240]
        return stem if len(stem)>=2 else 'Закупка из файла'

    @app.get('/assets/procurement.js',dependencies=[Depends(require_access)])
    def script():return FileResponse(Path(__file__).parent/'static/procurement.js',media_type='application/javascript')

    @app.get('/assets/price-memory.js',dependencies=[Depends(require_access)])
    def memory_script():return FileResponse(Path(__file__).parent/'static/price-memory.js',media_type='application/javascript')

    @app.get('/api/procurement/price-memory',dependencies=[Depends(require_access)])
    def price_memory(q:str='',specification:str='',region:str='',days:int=90,project_id:int|None=None,offset:int=0,limit:int=100):
        return call(memory.search,q,specification,region,days,project_id,offset,limit)

    @app.post('/api/procurement/price-memory/alerts/read',dependencies=[Depends(write_access)])
    def read_alerts(data:ReadAlerts):
        if any(not re.fullmatch('[a-f0-9]{64}',eid) for eid in data.event_ids):
            raise HTTPException(422,'Некорректный ID уведомления')
        actor=trusted_actor()
        with service.db.connection() as conn:
            for eid in data.event_ids:
                conn.execute('INSERT OR IGNORE INTO price_memory_alert_reads VALUES (?,?,?)',(actor,eid,utcnow()))
            service.db.audit('price_alerts_read','price_memory',actor,details={'count':len(data.event_ids)},conn=conn)
        return {'read':len(set(data.event_ids))}

    @app.post('/api/procurement/lots/{lid}/preview',dependencies=[Depends(write_access)])
    def preview(lid:int,data:CampaignCreate):return call(flow.preview,lid,data)

    def quick_draft_detail(row):
        data=json.loads(row['data_json'])
        if not data.get('quick_intake') or row['status']!='preview':
            raise HTTPException(404,'Незавершённый черновик не найден')
        document=service.db.one('SELECT * FROM source_documents WHERE id=?',
            (data['source_document_id'],))
        if not document:raise HTTPException(409,'Исходный файл черновика недоступен')
        return {'status':'needs_review','kind':'pdf' if row['kind']=='pdf_ocr' else 'sheet',
                'document':visible_document(document),'draft':{'preview_id':row['id'],**data},
                'reason':'Черновик сохранён. Проверьте отмеченные строки; письмо не отправлено'}

    @app.get('/api/procurement/quick-draft',dependencies=[Depends(require_access)])
    def latest_quick_draft(before:int|None=None):
        if before is not None and before<1:raise HTTPException(422,'Некорректная страница черновиков')
        rows=service.db.all('''SELECT rowid AS sequence,id,kind,data_json,status FROM launch_previews
            WHERE actor=? AND status='preview' AND kind IN ('lot_sheet','pdf_ocr')
              AND json_extract(data_json,'$.quick_intake')=1
              AND (? IS NULL OR rowid<?)
            ORDER BY rowid DESC LIMIT 51''',(trusted_actor(),before,before))
        page=rows[:50]
        pending=[]
        for row in page:
            data=json.loads(row['data_json'])
            document=service.db.one('SELECT filename FROM source_documents WHERE id=?',
                (data['source_document_id'],))
            pending.append({'preview_id':row['id'],
                'kind':'pdf' if row['kind']=='pdf_ocr' else 'sheet',
                'filename':document['filename'] if document else 'Исходный файл недоступен',
                'available':bool(document)})
        next_before=page[-1]['sequence'] if len(rows)>50 else None
        if before is not None:return {'pending_drafts':pending,'next_before':next_before}
        if not page:return None
        first_available=next((row for row,item in zip(page,pending) if item['available']),None)
        if not first_available:
            return {'status':'unavailable','pending_drafts':pending,'next_before':next_before,
                    'reason':'Исходные файлы незавершённых проверок недоступны; письма не отправлены'}
        return {**quick_draft_detail(first_available),'pending_drafts':pending,
                'next_before':next_before}

    @app.get('/api/procurement/quick-draft/{pid}',dependencies=[Depends(require_access)])
    def open_quick_draft(pid:str):
        row=service.db.one('''SELECT id,kind,data_json,status FROM launch_previews
            WHERE id=? AND actor=? AND kind IN ('lot_sheet','pdf_ocr')''',(pid,trusted_actor()))
        if not row:raise HTTPException(404,'Незавершённый черновик не найден')
        return quick_draft_detail(row)

    @app.post('/api/procurement/quick-draft/{pid}/remap',dependencies=[Depends(write_access)])
    def remap_quick_draft(pid:str,data:QuickMapping):
        allowed={'name','quantity','unit','specification','delivery_date'}
        if set(data.mapping)-allowed or not {'name','quantity','unit'}<=set(data.mapping):
            raise HTTPException(422,'Укажите колонки позиции, количества и единицы')
        with service.db.connection() as conn:
            row,stored=call(launch.preview,conn,pid,'lot_sheet')
        if not stored.get('quick_intake') or row['status']!='preview':
            raise HTTPException(409,'Черновик уже обработан или не принадлежит быстрой закупке')
        document=service.db.one('SELECT * FROM source_documents WHERE id=?',
            (stored['source_document_id'],))
        if not document:raise HTTPException(404,'Исходный файл не найден')
        content=call(launch.document_file,document)
        table=call(read_table,content,document['filename'],stored.get('sheet',''),1)
        if any(not isinstance(index,int) or isinstance(index,bool) or index<0 or index>=len(table['headers'])
               for index in data.mapping.values()):
            raise HTTPException(422,'Некорректный номер колонки')
        draft=call(launch.remap_quick_sheet_preview,pid,table,data.mapping,document)
        return {'status':'needs_review','kind':'sheet','document':visible_document(document),
                'draft':draft,'reason':'Проверьте распознанные строки перед отправкой'}

    @app.post('/api/procurement/quick-intake',dependencies=[Depends(write_access)])
    async def quick_intake(project_id:int=Form(...),file:UploadFile=File(...)):
        """Persist the original first; a readable sheet becomes a draft, never an email."""
        project=call(service.get_project,project_id)
        filename=file.filename or ''
        suffix=Path(filename).suffix.lower()
        if suffix not in {'.xlsx','.csv','.pdf'}:
            raise HTTPException(422,'Выберите PDF, XLSX или CSV')
        async with staged_upload(file) as content:
            document=await run_in_threadpool(call,lambda:service.register_source_document(
                filename=filename,content=content,document_type='project_section',project_id=project_id))
            if suffix in {'.xlsx','.csv'}:
                table=await run_in_threadpool(call,read_table,content,filename,'',1)
                if len(table['sheets'])!=1:
                    raise HTTPException(422,'XLSX содержит несколько листов. Выберите однолистную спецификацию: часть позиций нельзя молча пропустить. Исходный файл сохранён, письмо не отправлено')
                draft=await run_in_threadpool(call,lambda:launch.sheet_preview(
                    table,None,document,quick_intake=True))
                if draft.get('needs_mapping') or draft['errors'] or not draft['rows']:
                    return {'status':'needs_review','kind':'sheet','document':visible_document(document),'draft':draft,
                            'reason':'Проверьте отмеченные строки или сопоставление колонок; письмо не отправлено'}
                data={'project_id':project_id,'title':quick_lot_title(filename),
                      'region':project['region'],'delivery_address':project['delivery_address'],
                      'response_deadline':(date.today()+timedelta(days=7)).isoformat(),
                      'currency':'RUB','attachment_document_ids':[document['id']],
                      'items':[{k:r.get(k) for k in ('name','quantity','unit','specification','delivery_date')}
                               for r in draft['rows']]}
                lot=await run_in_threadpool(call,lambda:launch.create_sheet_lot(
                    draft['preview_id'],data,False,auto_draft=True))
                return {'status':'draft','lot':lot,'document':visible_document(document)}
            from .document_analysis import count_pdf_pages, extract_pdf_page_review
            from .pdf_ocr import candidate_rows, MAX_LINES, MAX_PAGES, MAX_REVIEW_LINES
            from .upload_io import FilePayload
            original=FilePayload(Path(document['storage_path']))
            page_count=await run_in_threadpool(call,count_pdf_pages,original)
            if not 1 <= page_count <= MAX_PAGES:
                raise HTTPException(422,'Быстрая закупка поддерживает PDF от 1 до 12 страниц; файл сохранён, письмо не отправлено')
            lines=[]
            for page in range(1,page_count+1):
                extracted=await run_in_threadpool(call,extract_pdf_page_review,original,page)
                page_lines=extracted['lines'] if extracted['mode']=='ocr' else [
                    {'text':text,'confidence':1.0}
                    for text in extracted['text'].splitlines() if text.strip()]
                if len(page_lines)>MAX_LINES:
                    raise HTTPException(422,'Слишком много строк на одном листе PDF; файл сохранён, письмо не отправлено')
                for line in page_lines:
                    lines.append({'line':len(lines)+1,'page':page,'text':line['text'],
                                  'confidence':line['confidence'],
                                  **({'bbox':line['bbox']} if line.get('bbox') else {})})
                if len(lines)>MAX_REVIEW_LINES:
                    raise HTTPException(422,'Слишком много строк в PDF; файл сохранён, письмо не отправлено')
            rows=candidate_rows(lines)
            if len(rows)>=500:
                raise HTTPException(422,'В PDF слишком много возможных позиций; файл сохранён, письмо не отправлено')
            pages={line['line']:line['page'] for line in lines}
            for row in rows:row['source_page']=pages[row['row']]
            draft=await run_in_threadpool(call,launch.save_preview,'pdf_ocr',{
                'source_document_id':document['id'],'source_sha256':document['sha256'],
                'project_id':project_id,'sheet':'1-'+str(page_count),'rows':rows,'lines':lines,
                'page_count':page_count,
                'quick_intake':True})
            return {'status':'needs_review','kind':'pdf','document':visible_document(document),'draft':draft,
                    'reason':'Распознано страниц: '+str(page_count)+'. Явные количества подставлены; проверьте исходник и исправьте только сомнительные строки. Письмо не отправлено'}

    @app.get('/api/procurement/campaigns/{cid}/snapshot',dependencies=[Depends(require_access)])
    def campaign_snapshot(cid:int):
        row=service.db.one('SELECT lot_id,snapshot_sha256 FROM rfq_snapshots WHERE campaign_id=?',(cid,))
        if not row:raise HTTPException(409,'Старому запросу нужен подтверждённый предпросмотр')
        return row

    @app.post('/api/procurement/outbox/{mid}/send',dependencies=[Depends(write_access)])
    def send(mid:int,data:Send,request:Request):
        if not getattr(request.state,'das_principal',None) and not session_claims(request.cookies.get('procurement_session','')):
            raise HTTPException(403,'Отправка требует личной сессии сотрудника')
        if not data.confirmed:raise HTTPException(422,'Подтвердите предпросмотр')
        with service.db.connection() as conn:
            message=call(service._outbox_context,conn,mid)
            call(validate_message,conn,message)
            snapshot=conn.execute('SELECT * FROM rfq_snapshots WHERE campaign_id=?',(message['campaign_id'],)).fetchone()
            if message['lot_id']!=data.lot_id or snapshot['snapshot_sha256']!=data.snapshot_sha256:
                raise HTTPException(409,'Запрос не принадлежит выбранному лоту / версии')
            needs_approval=call(flow.approval_required,conn,data.lot_id,role(request))
            if needs_approval and not flow.admin_approval_valid(conn,message):raise HTTPException(409,'Для этой закупки настроено отдельное согласование; требуется подтверждение текущего правила администратором')
        if message['status']=='draft':call(service.approve_message,mid,trusted_actor(),'Предпросмотр подтверждён действием Отправить запрос')
        return call(launch.send,mid,True)

    @app.post('/api/procurement/outbox/{mid}/approve',dependencies=[Depends(write_access)])
    def approve(mid:int,data:Confirm,request:Request):
        if role(request)!='admin':raise HTTPException(403,'Согласование правила закупки доступно администратору')
        if not data.confirmed:raise HTTPException(422,'Подтвердите согласование')
        with service.db.connection() as conn:
            message=call(service._outbox_context,conn,mid)
            call(validate_message,conn,message)
        return call(lambda:service.approve_message(mid,trusted_actor(),'Согласовано по правилу закупки',admin_policy_approval=True))

    @app.post('/api/procurement/lots/{lid}/comparison',dependencies=[Depends(write_access)])
    def comparison(lid:int,data:Confirm):
        if not data.confirmed:raise HTTPException(422,'Подтвердите переход к сравнению')
        with service.db.connection() as conn:
            if not conn.execute('SELECT 1 FROM quotes WHERE lot_id=?',(lid,)).fetchone():raise HTTPException(409,'Для сравнения нужны полученные цены')
            service._set_lot_progress(conn,lid,'comparison')
            service.db.audit('comparison_opened','lot',lid,conn=conn)
        return service.get_lot(lid)

    @app.get('/api/procurement/policy',dependencies=[Depends(require_access)])
    def policy():
        row=service.db.one('SELECT * FROM procurement_policy WHERE id=1')
        return {'amount_threshold':row['amount_threshold'] if row else None,'currency':row['currency'] if row else 'RUB',
            'required_roles':json.loads(row['required_roles_json']) if row else []}

    @app.put('/api/procurement/policy',dependencies=[Depends(write_access)])
    def save_policy(data:Policy,request:Request):
        if role(request)!='admin':raise HTTPException(403,'Изменение правил доступно администратору')
        if set(data.required_roles)-{'staff','admin'}:raise HTTPException(422,'Неизвестная роль')
        threshold=call(number,data.amount_threshold) if data.amount_threshold is not None else None
        with service.db.connection() as conn:
            conn.execute('INSERT OR REPLACE INTO procurement_policy VALUES (1,?,?,?,?)',(threshold,data.currency,json.dumps(data.required_roles),utcnow()))
            service.db.audit('approval_policy_changed','procurement_policy',1,details=data.model_dump(),conn=conn)
        return policy()

    @app.post('/api/procurement/lots/{lid}/decision',dependencies=[Depends(write_access)])
    def decide(lid:int,data:Decision):
        with service.db.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            if not conn.execute('SELECT 1 FROM quotes WHERE id=? AND lot_id=?',(data.quote_id,lid)).fetchone():raise HTTPException(409,'КП не принадлежит закупке')
            previous=conn.execute('SELECT * FROM procurement_decisions WHERE lot_id=?',(lid,)).fetchone()
            if previous and previous['stage']=='ordered':
                if data.stage=='ordered' and previous['quote_id']==data.quote_id:
                    return service.get_lot(lid)  # idempotent, no rewritten order/audit
                raise HTTPException(409,'Заказ уже зафиксирован. Изменение поставщика или отмена требуют отдельной подтверждённой корректировки')
            if data.stage=='ordered' and (not previous or previous['quote_id']!=data.quote_id):raise HTTPException(409,'Сначала выберите поставщика')
            conn.execute('INSERT OR REPLACE INTO procurement_decisions VALUES (?,?,?,?,?)',(lid,data.quote_id,data.stage,trusted_actor(),utcnow()))
            conn.execute('UPDATE lots SET status=? WHERE id=?',(data.stage,lid))
            service.db.audit('procurement_'+data.stage,'lot',lid,details={'quote_id':data.quote_id},conn=conn)
        return service.get_lot(lid)

    @app.get('/api/procurement/catalog',dependencies=[Depends(require_access)])
    def prices(q:str='',specification:str=''):return call(catalog.prices,q,specification)

    @app.post('/api/procurement/catalog/preview',dependencies=[Depends(write_access)])
    async def price_preview(file:UploadFile=File(...),mapping:str=Form(''),sheet:str=Form(''),header_row:int=Form(1)):
        async with staged_upload(file) as content:
            suffix=Path(file.filename or '').suffix.lower()
            if suffix=='.pdf':
                from .price_ocr import extract_price_document as extract_document
                result=await run_in_threadpool(call,extract_document,content,file.filename)
                doc=await run_in_threadpool(call,lambda:service.register_source_document(filename=file.filename,content=content,document_type='price_list',_price_import=True))
                return await run_in_threadpool(call,catalog.extracted_price_preview,doc,result)
            table=await run_in_threadpool(call,read_table,content,file.filename,sheet,header_row)
            doc=await run_in_threadpool(call,lambda:service.register_source_document(filename=file.filename,content=content,document_type='price_list',_price_import=True))
            return await run_in_threadpool(call,catalog.price_preview,doc,table,json.loads(mapping) if mapping else None)

    @app.post('/api/procurement/catalog/{pid}/apply',dependencies=[Depends(write_access)])
    def apply_prices(pid:str,data:Confirm):return call(catalog.apply_prices,pid,data.confirmed)

    @app.post('/api/procurement/catalog/{pid}/review-pdf',dependencies=[Depends(write_access)])
    def review_pdf(pid:str,data:ReviewedPriceRows):
        if any(set(r)-set(ALIASES) or any(len(v)>8000 for v in r.values()) for r in data.rows):
            raise HTTPException(422,'Некорректные поля прайса')
        return call(catalog.review_pdf,pid,data.rows,data.confirmed_rub)

    @app.post('/api/procurement/catalog/incoming-mail',dependencies=[Depends(write_access)])
    async def incoming(file:UploadFile=File(...)):
        from email import policy
        from email.parser import BytesParser
        from .upload_io import open_payload,payload_sha256
        from .table_ingest import safe_upload
        from .price_ocr import extract_price_document as extract_document
        async with staged_upload(file) as content:
            if Path(file.filename or '').suffix.lower()!='.eml' or len(content)>20*1024*1024:
                raise HTTPException(422,'Ожидается исходное входящее письмо EML не больше 20 МБ')
            with open_payload(content) as stream:
                mail=await run_in_threadpool(BytesParser(policy=policy.default).parse,stream)
            result=[]
            for part in mail.iter_attachments():
                name=part.get_filename() or ''
                if Path(name).suffix.lower() not in {'.pdf','.xlsx','.csv'}:continue
                if len(result)>=20:raise HTTPException(422,'Не больше 20 вложений')
                raw=part.get_payload(decode=True)
                call(safe_upload,raw,name,{'.pdf','.xlsx','.csv'})
                doc=await run_in_threadpool(call,lambda:service.register_source_document(filename=name,content=raw,document_type='price_list',_price_import=True))
                if Path(name).suffix.lower()=='.pdf':
                    extraction=await run_in_threadpool(call,extract_document,raw,name)
                    preview=await run_in_threadpool(call,catalog.extracted_price_preview,doc,extraction)
                else:
                    table=await run_in_threadpool(call,read_table,raw,name)
                    preview=await run_in_threadpool(call,catalog.price_preview,doc,table)
                result.append(preview)
            if not result:raise HTTPException(422,'В письме нет поддерживаемых прайсов')
            service.db.audit('incoming_prices_previewed','mail',payload_sha256(content),details={'attachments':len(result)})
            return {'previews':result}

    @app.get('/api/procurement/projects/{pid}',dependencies=[Depends(require_access)])
    def portfolio(pid:int):return call(catalog.portfolio,pid)

    @app.put('/api/procurement/projects/{pid}/budget',dependencies=[Depends(write_access)])
    def budget(pid:int,data:Budget):
        call(service.get_project,pid)
        amount=call(number,data.amount)
        with service.db.connection() as conn:
            conn.execute('INSERT OR REPLACE INTO project_budgets VALUES (?,?,?,?,?)',(pid,data.currency,amount,trusted_actor(),utcnow()))
            service.db.audit('budget_changed','project',pid,details={'currency':data.currency,'amount':amount},conn=conn)
        return portfolio(pid)

    @app.post('/api/procurement/projects/{pid}/workbook',dependencies=[Depends(write_access)])
    async def workbook(pid:int,file:UploadFile=File(...),confirmed:bool=Form(False),expected_sha256:str=Form('')):
        call(service.get_project,pid)
        async with staged_upload(file) as content:
            from .upload_io import payload_sha256
            source_hash=payload_sha256(content)
            rows=await run_in_threadpool(call,workbook_rows,content,file.filename)
            if not confirmed:return {'rows':rows,'filename':file.filename,'sha256':source_hash}
            if expected_sha256!=source_hash:raise HTTPException(409,'Исходник изменился после предпросмотра; повторите проверку')
            doc=await run_in_threadpool(call,lambda:service.register_source_document(filename=file.filename,content=content,document_type='project_section',project_id=pid))
            return await run_in_threadpool(call,catalog.import_workbook,doc,rows,confirmed)

    @app.api_route('/api/procurement/documents/{did}/view',methods=['GET','HEAD'],dependencies=[Depends(require_access)])
    def view(did:int,sheet:str=''):
        doc=service.db.one('SELECT * FROM source_documents WHERE id=?',(did,))
        if not doc:raise HTTPException(404,'Документ не найден')
        content=call(launch.document_file,doc)
        suffix=Path(doc['filename']).suffix.lower()
        headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff',
            'Content-Security-Policy':"default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'self'; sandbox allow-same-origin"}
        if suffix in {'.pdf','.png','.jpg','.jpeg'}:
            # Chromium's native PDF viewer is disabled by CSP sandbox. Only
            # immutable, validated binary types use this route; Office HTML
            # retains the sandbox below. Auth and same-origin framing remain.
            headers['Content-Security-Policy']="default-src 'none'; frame-ancestors 'self'"
            return FileResponse(doc['storage_path'],media_type={'.pdf':'application/pdf','.png':'image/png','.jpg':'image/jpeg','.jpeg':'image/jpeg'}[suffix],headers=headers)
        from .document_viewer import office_html
        return HTMLResponse(call(office_html,content,doc['filename'],sheet),headers=headers)
