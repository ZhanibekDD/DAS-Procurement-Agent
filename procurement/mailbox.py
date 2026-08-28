from __future__ import annotations

import hashlib
import imaplib
import re
import smtplib
import ssl
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email import policy
from email.message import EmailMessage, Message
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime, parseaddr
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable


_REFERENCE_RE = re.compile(r"<[^<>\r\n]+>")


class MailboxError(RuntimeError):
    pass


@dataclass(frozen=True)
class MailAttachment:
    filename: str
    content_type: str
    content: bytes


@dataclass(frozen=True)
class InboundMail:
    message_id: str
    sender: str
    recipients: tuple[str, ...]
    cc: tuple[str, ...]
    subject: str
    body_text: str
    sent_at: str | None
    in_reply_to: str
    references: tuple[str, ...]
    thread_key: str
    attachments: tuple[MailAttachment, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class MailboxFetch:
    uidvalidity: str
    messages: tuple[tuple[int, InboundMail], ...]
    highest_uid: int


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.parts.append(data.strip())

    def text(self) -> str:
        return "\n".join(self.parts)


def _addresses(message: Message, header: str) -> tuple[str, ...]:
    values = message.get_all(header, [])
    result = []
    for _, address in getaddresses(values):
        value = address.strip().lower()
        if value and "@" in value and value not in result:
            result.append(value)
    return tuple(result)


def _plain_body(message: Message) -> str:
    plain: list[str] = []
    html: list[str] = []
    parts: Iterable[Message] = message.walk() if message.is_multipart() else (message,)
    for part in parts:
        if part.is_multipart() or part.get_filename():
            continue
        disposition = (part.get_content_disposition() or "").lower()
        if disposition == "attachment":
            continue
        content_type = part.get_content_type().lower()
        if content_type not in {"text/plain", "text/html"}:
            continue
        try:
            value = part.get_content()
        except (LookupError, UnicodeError):
            payload = part.get_payload(decode=True) or b""
            value = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        if not isinstance(value, str):
            continue
        if content_type == "text/plain":
            plain.append(value.strip())
        else:
            html.append(value)
    if any(plain):
        return "\n\n".join(value for value in plain if value).strip()
    extractor = _TextExtractor()
    for value in html:
        extractor.feed(value)
    return extractor.text().strip()


def _attachments(message: Message) -> tuple[MailAttachment, ...]:
    result = []
    for part in message.walk():
        filename = part.get_filename()
        disposition = (part.get_content_disposition() or "").lower()
        if not filename and disposition != "attachment":
            continue
        payload = part.get_payload(decode=True) or b""
        safe_name = Path(str(filename or "attachment.bin")).name.replace("\x00", "")
        result.append(
            MailAttachment(
                filename=safe_name[:255] or "attachment.bin",
                content_type=part.get_content_type().lower(),
                content=payload,
            )
        )
    return tuple(result)


def parse_inbound_mail(raw: bytes) -> InboundMail:
    message = BytesParser(policy=policy.default).parsebytes(raw)
    sender = parseaddr(str(message.get("From", "")))[1].strip().lower()
    if not sender or "@" not in sender:
        raise MailboxError("incoming message has no valid sender")

    raw_message_id = str(message.get("Message-ID", "")).strip()
    message_id = raw_message_id if _REFERENCE_RE.fullmatch(raw_message_id) else ""
    if not message_id:
        message_id = f"<{hashlib.sha256(raw).hexdigest()}@missing-message-id.invalid>"

    in_reply_to_values = _REFERENCE_RE.findall(str(message.get("In-Reply-To", "")))
    in_reply_to = in_reply_to_values[-1] if in_reply_to_values else ""
    references = tuple(_REFERENCE_RE.findall(str(message.get("References", ""))))
    thread_key = references[0] if references else in_reply_to or message_id

    sent_at = None
    raw_date = str(message.get("Date", "")).strip()
    if raw_date:
        try:
            parsed = parsedate_to_datetime(raw_date)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            sent_at = parsed.astimezone(UTC).replace(microsecond=0).isoformat()
        except (TypeError, ValueError, OverflowError):
            sent_at = None

    return InboundMail(
        message_id=message_id,
        sender=sender,
        recipients=_addresses(message, "To"),
        cc=_addresses(message, "Cc"),
        subject=str(message.get("Subject", "")).strip()[:500],
        body_text=_plain_body(message)[:50000],
        sent_at=sent_at,
        in_reply_to=in_reply_to,
        references=references,
        thread_key=thread_key,
        attachments=_attachments(message),
    )


class ImapMailbox:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str,
        password: str,
        timeout: int = 30,
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.timeout = timeout

    def fetch_since(self, last_uid: int, *, limit: int = 100) -> MailboxFetch:
        client: imaplib.IMAP4_SSL | None = None
        try:
            client = imaplib.IMAP4_SSL(
                self.host,
                self.port,
                ssl_context=ssl.create_default_context(),
                timeout=self.timeout,
            )
            client.login(self.username, self.password)
            status, _ = client.select("INBOX", readonly=True)
            if status != "OK":
                raise MailboxError("cannot select INBOX")

            uidvalidity_response = client.response("UIDVALIDITY")[1] or []
            uidvalidity = (
                uidvalidity_response[0].decode("ascii", errors="ignore")
                if uidvalidity_response
                else ""
            )
            status, data = client.uid("search", None, "ALL")
            if status != "OK":
                raise MailboxError("cannot search INBOX")
            all_uids = [int(value) for value in (data[0] or b"").split()]
            selected = [uid for uid in all_uids if uid > last_uid][:limit]
            messages: list[tuple[int, InboundMail]] = []
            for uid in selected:
                status, payload = client.uid("fetch", str(uid), "(BODY.PEEK[])")
                if status != "OK":
                    raise MailboxError(f"cannot fetch IMAP UID {uid}")
                raw = next(
                    (
                        item[1]
                        for item in payload
                        if isinstance(item, tuple) and isinstance(item[1], bytes)
                    ),
                    None,
                )
                if raw is None:
                    raise MailboxError(f"IMAP UID {uid} has no message body")
                messages.append((uid, parse_inbound_mail(raw)))
            highest_uid = max(selected, default=last_uid)
            return MailboxFetch(uidvalidity, tuple(messages), highest_uid)
        except (imaplib.IMAP4.error, OSError, ssl.SSLError) as exc:
            raise MailboxError("IMAP synchronization failed") from exc
        finally:
            if client is not None:
                try:
                    client.logout()
                except (imaplib.IMAP4.error, OSError):
                    pass


class SmtpMailbox:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str,
        password: str,
        timeout: int = 30,
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.timeout = timeout

    def send(
        self,
        *,
        sender: str,
        recipient: str,
        subject: str,
        body: str,
        message_id: str,
        in_reply_to: str = "",
        references: Iterable[str] = (),
        attachments: Iterable[dict[str, object]] = (),
    ) -> None:
        message = EmailMessage(policy=policy.SMTP)
        message["From"] = sender
        message["To"] = recipient
        message["Subject"] = subject
        message["Message-ID"] = message_id
        if in_reply_to:
            message["In-Reply-To"] = in_reply_to
        reference_values = [str(value) for value in references if value]
        if reference_values:
            message["References"] = " ".join(reference_values)
        message.set_content(body)
        for attachment in attachments:
            content = bytes(attachment["content"])
            content_type = str(attachment.get("content_type") or "application/octet-stream")
            maintype, _, subtype = content_type.partition("/")
            message.add_attachment(
                content,
                maintype=maintype or "application",
                subtype=subtype or "octet-stream",
                filename=Path(str(attachment["filename"])).name,
            )

        try:
            with smtplib.SMTP_SSL(
                self.host,
                self.port,
                timeout=self.timeout,
                context=ssl.create_default_context(),
            ) as client:
                client.login(self.username, self.password)
                client.send_message(message)
        except (smtplib.SMTPException, OSError, ssl.SSLError) as exc:
            raise MailboxError("SMTP delivery failed") from exc
