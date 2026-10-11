"""Bounded disk-backed uploads and streaming checksums. No credentials here."""
import asyncio
import hashlib
import io
import re
import tempfile
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

MAX_FILE = 100 * 1024 * 1024
MAX_BATCH = MAX_FILE
MAX_BODY = MAX_BATCH + 1024 * 1024  # bounded multipart framing, not file allowance
MAX_PRICE_REVIEW_BODY = 2 * 1024 * 1024
CHUNK = 1024 * 1024
TOO_LARGE = 'Файл больше 100 МБ'
PACK_TOO_LARGE = 'Пакет файлов больше 100 МБ; загружайте по одному'


class UploadTooLarge(ValueError):
    pass


@dataclass(frozen=True)
class FilePayload:
    path: Path

    def __len__(self):
        return self.path.stat().st_size

    def startswith(self, prefix):
        with self.path.open('rb') as stream:
            return stream.read(len(prefix)).startswith(prefix)


def open_payload(content):
    return content.path.open('rb') if isinstance(content,FilePayload) else io.BytesIO(content)


def chunks(content):
    with open_payload(content) as stream:
        while part := stream.read(CHUNK):
            yield part


def payload_sha256(content):
    digest=hashlib.sha256()
    for part in chunks(content):digest.update(part)
    return digest.hexdigest()


@asynccontextmanager
async def staged_upload(file, limit=MAX_FILE):
    # Each request owns its temporary directory; cleanup includes cancellation.
    with tempfile.TemporaryDirectory(prefix='procurement-upload-') as directory:
        path=Path(directory)/'payload'
        size=0
        with path.open('xb') as destination:
            while part := await file.read(CHUNK):
                size+=len(part)
                if size>limit:
                    raise UploadTooLarge(TOO_LARGE if limit==MAX_FILE else PACK_TOO_LARGE)
                task=asyncio.create_task(asyncio.to_thread(destination.write,part))
                try:await asyncio.shield(task)
                except asyncio.CancelledError:
                    await task  # finish the bounded write before closing/removing
                    raise
        if not size:raise ValueError('Файл пуст')
        yield FilePayload(path)


def upload_request(scope):
    return (scope.get('type')=='http' and scope.get('method')=='POST' and
            (scope.get('path') in {'/api/documents','/api/imports/batch','/api/suppliers/import',
                '/api/launch/lot-sheet/preview','/api/launch/supplier-import/preview',
                '/api/procurement/catalog/preview','/api/procurement/catalog/incoming-mail',
                '/api/procurement/quick-intake'}
             or bool(re.fullmatch(r'/api/procurement/projects/\d+/workbook',scope.get('path','')))))


def price_review_request(scope):
    return (scope.get('type')=='http' and scope.get('method')=='POST' and
            bool(re.fullmatch(r'/api/procurement/catalog/[^/]+/review-pdf',scope.get('path',''))))


class UploadBodyLimit:
    """Bound upload and price-review bodies before parsing, including chunks."""
    def __init__(self,app):
        self.app=app
        self.slots=asyncio.BoundedSemaphore(2)

    async def __call__(self,scope,receive,send):
        if price_review_request(scope):
            return await self._price_review(scope,receive,send)
        if not upload_request(scope):return await self.app(scope,receive,send)
        from starlette.formparsers import MultiPartException
        from starlette.responses import JSONResponse
        length=dict(scope.get('headers',[])).get(b'content-length',b'0')
        error=PACK_TOO_LARGE if scope['path']=='/api/imports/batch' else TOO_LARGE
        try:too_large=int(length)>MAX_BODY
        except ValueError:too_large=True
        if too_large:return await JSONResponse({'detail':error},413)(scope,receive,send)
        try:await asyncio.wait_for(self.slots.acquire(),timeout=0.05)
        except TimeoutError:
            return await JSONResponse({'detail':'Загрузка занята; повторите позже'},429)(scope,receive,send)
        total=0
        exceeded=False
        async def bounded_receive():
            nonlocal total,exceeded
            message=await receive()
            if message['type']=='http.request':
                total+=len(message.get('body',b''))
                if total>MAX_BODY:
                    exceeded=True
                    # Starlette closes partially spooled files for this exception.
                    raise MultiPartException(error)
            return message
        async def bounded_send(message):
            if exceeded and message['type']=='http.response.start' and message['status']==400:
                message={**message,'status':413}
            await send(message)
        try:return await self.app(scope,bounded_receive,bounded_send)
        finally:self.slots.release()

    async def _price_review(self,scope,receive,send):
        from starlette.responses import JSONResponse
        error='Проверка прайса превышает 2 МБ; разделите файл или сократите поля'
        length=dict(scope.get('headers',[])).get(b'content-length',b'0')
        try:too_large=int(length)>MAX_PRICE_REVIEW_BODY
        except ValueError:too_large=True
        if too_large:return await JSONResponse({'detail':error},413)(scope,receive,send)
        try:await asyncio.wait_for(self.slots.acquire(),timeout=0.05)
        except TimeoutError:
            return await JSONResponse({'detail':'Проверка прайса занята; повторите позже'},429)(scope,receive,send)
        try:
            body=bytearray()
            try:
                async with asyncio.timeout(30):
                    while True:
                        message=await receive()
                        if message['type']=='http.disconnect':return
                        if message['type']!='http.request':continue
                        chunk=message.get('body',b'')
                        if len(body)+len(chunk)>MAX_PRICE_REVIEW_BODY:
                            return await JSONResponse({'detail':error},413)(scope,receive,send)
                        body.extend(chunk)
                        if not message.get('more_body',False):break
            except TimeoutError:
                return await JSONResponse({'detail':'Время передачи прайса истекло; повторите загрузку'},408)(scope,receive,send)
            pending=True
            async def replay_receive():
                nonlocal pending
                if pending:
                    pending=False
                    return {'type':'http.request','body':bytes(body),'more_body':False}
                return await receive()
            return await self.app(scope,replay_receive,send)
        finally:self.slots.release()
