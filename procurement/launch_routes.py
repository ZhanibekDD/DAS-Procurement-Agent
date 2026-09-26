"""Authenticated HTTP surface for the bounded launch scope."""
import hashlib
import hmac
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool
from pydantic import Field, StrictInt
from .models import StrictModel, SupplierCreate, LotCreate
from .table_ingest import read_table, MAX_FILE
from .upload_io import staged_upload


class Confirm(StrictModel):
    confirmed: bool = Field(strict=True)


class SupplierEdit(SupplierCreate):
    revision: str = Field(pattern=r'^[a-f0-9]{64}$')


class SupplierState(Confirm):
    revision: str = Field(pattern=r'^[a-f0-9]{64}$')


class AttachmentChoice(StrictModel):
    document_ids: list[int] = Field(max_length=30)


class SheetConfirm(Confirm):
    lot: LotCreate


class PriceRejection(StrictModel):
    entry_ids: list[StrictInt] = Field(min_length=1, max_length=500)
    rejected_by: str = Field(min_length=1, max_length=128)


def install(app, settings, service, launch, require_access, session_claims, domain_error):
    async def write_access(request: Request, _: None = Depends(require_access)):
        if settings.sso_enabled:
            return  # backchannel permission and CSRF already checked by middleware
        if settings.api_key and hmac.compare_digest(request.headers.get('x-api-key',''), settings.api_key):
            return
        cookie = request.cookies.get('procurement_session','')
        if not session_claims(cookie):
            if settings.environment != 'production' and not settings.local_auth_configured and not settings.api_key:
                return
            raise HTTPException(403,'Сессия необходима')
        expected = hmac.new(settings.auth_secret.encode(),('launch:' + cookie).encode(),hashlib.sha256).hexdigest()
        if not hmac.compare_digest(request.headers.get('x-launch-csrf-token',''),expected):
            raise HTTPException(403,'CSRF проверка не пройдена')

    def call(fn, *args):
        try:
            return fn(*args)
        except Exception as exc:
            raise domain_error(exc) from None

    @app.get('/assets/launch.js', dependencies=[Depends(require_access)])
    def script():
        return FileResponse(Path(__file__).parent / 'static' / 'launch.js',media_type='application/javascript')

    @app.get('/api/launch/config', dependencies=[Depends(require_access)])
    def config(request: Request):
        return {'smtp_ready':bool(os.getenv('PROCUREMENT_SMTP_HOST') and os.getenv('PROCUREMENT_SMTP_FROM')),
                'read_only':bool(getattr(request.state,'das_principal',{}).get('read_only',False)),
                'max_file_bytes':MAX_FILE}

    @app.post('/api/launch/imports/{batch_id}/reject', dependencies=[Depends(write_access)])
    def reject_prices(batch_id: int, data: PriceRejection):
        return call(service.reject_batch_entries, batch_id, data.entry_ids, data.rejected_by)

    @app.get('/api/launch/suppliers', dependencies=[Depends(require_access)])
    def deleted_suppliers():
        return [launch.supplier(row['id']) for row in service.db.all('SELECT id FROM suppliers WHERE active=0 ORDER BY id DESC')]

    @app.get('/api/launch/suppliers/{sid}', dependencies=[Depends(require_access)])
    def supplier(sid: int):
        return call(launch.supplier,sid)

    @app.put('/api/launch/suppliers/{sid}', dependencies=[Depends(write_access)])
    def edit(sid: int, data: SupplierEdit):
        values = data.model_dump(mode='json')
        revision = values.pop('revision')
        return call(launch.edit_supplier,sid,values,revision)

    @app.delete('/api/launch/suppliers/{sid}', dependencies=[Depends(write_access)])
    def delete(sid: int, data: SupplierState):
        return call(launch.supplier_state,sid,False,data.revision,data.confirmed)

    @app.post('/api/launch/suppliers/{sid}/restore', dependencies=[Depends(write_access)])
    def restore(sid: int, data: SupplierState):
        return call(launch.supplier_state,sid,True,data.revision,data.confirmed)

    @asynccontextmanager
    async def table(file, mapping, sheet, header_row):
        try:
            async with staged_upload(file) as content:
                parsed = await run_in_threadpool(read_table,content,file.filename or '',sheet,header_row)
                chosen = json.loads(mapping) if mapping else None
                if chosen is not None and not isinstance(chosen,dict):
                    raise ValueError('Сопоставление должно быть объектом')
                yield parsed,chosen,content
        except HTTPException:
            raise
        except Exception as exc:
            raise domain_error(exc) from None

    @app.post('/api/launch/supplier-import/preview', dependencies=[Depends(write_access)])
    async def preview_suppliers(file: UploadFile=File(...), mapping: str=Form(''),
                                sheet: str=Form(''), header_row: int=Form(1), region: str=Form('Воронежская область')):
        async with table(file,mapping,sheet,header_row) as (parsed,chosen,_):
            return await run_in_threadpool(call,launch.supplier_preview,parsed,chosen,region)

    @app.post('/api/launch/supplier-import/{pid}/apply', dependencies=[Depends(write_access)])
    def apply(pid: str,data: Confirm):
        return call(launch.apply_import,pid,data.confirmed)

    @app.post('/api/launch/supplier-import/{pid}/rollback', dependencies=[Depends(write_access)])
    def rollback(pid: str,data: Confirm):
        return call(launch.rollback_import,pid,data.confirmed)

    @app.get('/api/launch/imports', dependencies=[Depends(require_access)])
    def history():
        from .identity import trusted_actor
        rows=service.db.all("SELECT id,status,result_json,created_at FROM launch_previews WHERE kind='supplier_import' AND actor=? ORDER BY created_at DESC LIMIT 100",(trusted_actor(),))
        return [{k:v for k,v in r.items() if k!='result_json'} | {'report':json.loads(r['result_json'] or '{}')} for r in rows]

    @app.post('/api/launch/lot-sheet/preview', dependencies=[Depends(write_access)])
    async def preview_lot(file: UploadFile=File(...),mapping: str=Form(''),sheet: str=Form(''),header_row: int=Form(1),project_id: int=Form(...)):
        async with table(file,mapping,sheet,header_row) as (parsed,chosen,content):
            document = await run_in_threadpool(call,lambda: service.register_source_document(filename=file.filename or '',content=content,
                             document_type='project_section',project_id=project_id))
            return await run_in_threadpool(call,launch.sheet_preview,parsed,chosen,document)

    @app.post('/api/launch/lot-sheet/{pid}/create', dependencies=[Depends(write_access)],status_code=201)
    def create(pid: str,data: SheetConfirm):
        return call(launch.create_sheet_lot,pid,data.lot.model_dump(mode='json'),data.confirmed)

    @app.put('/api/launch/lots/{lid}/attachments', dependencies=[Depends(write_access)])
    def attachments(lid: int,data: AttachmentChoice):
        return call(launch.attach_lot,lid,data.document_ids)

    @app.api_route('/api/launch/documents/{did}/download',methods=['GET','HEAD'],dependencies=[Depends(require_access)])
    def download(did: int):
        doc=service.db.one('SELECT * FROM source_documents WHERE id=?',(did,))
        if not doc:
            raise HTTPException(404,'Файл не найден')
        call(launch.document_file,doc)
        service.db.audit('downloaded','source_document',did)
        return FileResponse(doc['storage_path'],filename=doc['filename'],media_type='application/octet-stream',headers={'X-Content-Type-Options':'nosniff','Cache-Control':'no-store'})

    @app.post('/api/launch/outbox/{mid}/send',dependencies=[Depends(write_access)])
    def send(mid: int,data: Confirm,request: Request):
        if not getattr(request.state,'das_principal',None) and not session_claims(request.cookies.get('procurement_session','')):
            raise HTTPException(403,'Отправка требует личной сессии сотрудника')
        return call(launch.send,mid,data.confirmed)
