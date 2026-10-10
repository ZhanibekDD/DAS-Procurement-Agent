"""Shared corporate mailbox is administrator-only, never authorized by From."""
from pathlib import Path
from fastapi import Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import Field
from .identity import trusted_role
from .models import StrictModel
from .incoming_mail import Inbox, MAX_ROWS
from . import inbox_quotes
from .inbox_quotes import InboxQuote


class Review(StrictModel):
    rows: list[dict[str,str]] = Field(min_length=1, max_length=MAX_ROWS)
    confirmed_source: bool = Field(strict=True)


def install(app, service, launch, require_access, write_access, call):
    inbox = Inbox(service, launch)

    def administrator(_: None = Depends(require_access)):
        if trusted_role() != 'admin':
            raise HTTPException(403, 'Входящие письма общего ящика доступны только администратору')

    @app.get('/api/procurement/inbox', dependencies=[Depends(administrator)])
    def listing(before: int | None = None):
        return call(inbox.listing, before)

    @app.get('/api/procurement/inbox/attachments/{aid}', dependencies=[Depends(administrator)])
    def detail(aid: str):
        return call(inbox.detail, aid)

    @app.post('/api/procurement/inbox/attachments/{aid}/recognize', dependencies=[Depends(administrator), Depends(write_access)])
    def recognize(aid: str):
        return call(inbox.recognize, aid)

    @app.post('/api/procurement/inbox/attachments/{aid}/review', dependencies=[Depends(administrator), Depends(write_access)])
    def review(aid: str, data: Review):
        return call(inbox.prepare, aid, data.rows, data.confirmed_source)

    @app.get('/api/procurement/inbox/attachments/{aid}/quote-draft', dependencies=[Depends(administrator)])
    def quote_draft(aid: str, lot_id: int):
        return call(inbox_quotes.quote_draft, service, inbox, aid, lot_id)

    @app.post('/api/procurement/inbox/attachments/{aid}/quote', dependencies=[Depends(administrator), Depends(write_access)])
    def save_quote(aid: str, data: InboxQuote):
        return call(inbox_quotes.save_quote, service, inbox, aid, data)

    @app.api_route('/api/procurement/inbox/attachments/{aid}/original', methods=['GET','HEAD'], dependencies=[Depends(administrator)])
    def original(aid: str, request: Request):
        attachment = call(inbox.attachment, aid)
        path = call(inbox.path, attachment)
        suffix = Path(attachment['filename']).suffix.lower()
        headers = {'Cache-Control':'no-store', 'X-Content-Type-Options':'nosniff',
                   'Content-Security-Policy': "default-src 'none'; frame-ancestors 'self'"}
        service.db.audit('incoming_original_opened', 'inbox_attachment', aid)
        return FileResponse(path, filename=attachment['filename'],
            content_disposition_type='inline' if suffix=='.pdf' else 'attachment',
            media_type='application/pdf' if suffix=='.pdf' else 'application/octet-stream', headers=headers)
