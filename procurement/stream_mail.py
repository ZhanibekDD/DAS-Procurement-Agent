"""Bounded MIME encoding and SMTP DATA, including SMTP dot transparency."""
import base64
import hashlib
import mimetypes
import smtplib
import tempfile
import uuid
import os
from pathlib import Path
from email import policy
from email.message import EmailMessage

from .upload_io import CHUNK, open_payload


class SubmissionUncertain(Exception):
    """DATA transmission began but final acceptance is not known. Never resend."""


def spool_message(path, email, attachments):
    path = Path(path)
    with tempfile.TemporaryDirectory(prefix='.mail-', dir=path.parent) as directory:
        temporary = Path(directory) / 'message.eml'
        _encode_message(temporary, email, attachments)
        # link is atomic and exclusive (unlike replace). Never overwrite old MIME.
        os.link(temporary, path)


def _encode_message(path, email, attachments):
    # MIME is spooled to disk, not accumulated in EmailMessage/SMTP.sendmail.
    path = Path(path)
    # Exclusive immutable spool: the exact SMTP bytes are also the Sent copy.
    with path.open('xb') as message:
        os.chmod(path, 0o600)
        boundary='procurement-'+uuid.uuid4().hex
        body=email.get_content()
        email.clear_content()
        if 'MIME-Version' not in email:email['MIME-Version']='1.0'
        email['Content-Type']='multipart/mixed; boundary="'+boundary+'"'
        message.write(email.as_bytes(policy=policy.SMTP).split(b'\r\n\r\n',1)[0]+b'\r\n\r\n')
        message.write(b'--'+boundary.encode()+b'\r\n')
        part=EmailMessage();part.set_content(body)
        message.write(part.as_bytes(policy=policy.SMTP))
        for filename,payload,expected_sha in attachments:
            part=EmailMessage()
            part['Content-Type']=mimetypes.guess_type(filename)[0] or 'application/octet-stream'
            part['Content-Transfer-Encoding']='base64'
            part.add_header('Content-Disposition','attachment',filename=filename)
            message.write(b'\r\n--'+boundary.encode()+b'\r\n')
            message.write(part.as_bytes(policy=policy.SMTP))
            digest=hashlib.sha256()
            with open_payload(payload) as source:
                while chunk:=source.read(57*1024):
                    digest.update(chunk)
                    encoded=base64.b64encode(chunk)
                    message.write(b''.join(encoded[offset:offset+76]+b'\r\n'
                                          for offset in range(0,len(encoded),76)))
            if digest.hexdigest()!=expected_sha:
                raise ValueError('Attachment changed during MIME encoding')
        message.write(b'\r\n--'+boundary.encode()+b'--\r\n')
        message.flush()
        os.fsync(message.fileno())


def submit_spool(smtp, path, sender, recipient):
    with Path(path).open('rb') as message:
        size = Path(path).stat().st_size
        smtp.ehlo_or_helo_if_needed()
        options=[]
        if smtp.has_extn('size'):
            maximum=smtp.esmtp_features.get('size','').strip()
            if maximum.isdigit() and size>int(maximum):
                raise ValueError('SMTP provider message-size limit exceeded')
            options=['size='+str(size)]
        code,reply=smtp.mail(sender,options)
        if code!=250:raise smtplib.SMTPSenderRefused(code,reply,sender)
        code,reply=smtp.rcpt(recipient)
        if code not in (250,251):
            smtp.rset()
            raise smtplib.SMTPRecipientsRefused({recipient:(code,reply)})
        code,reply=smtp.docmd('DATA')
        if code!=354:raise smtplib.SMTPDataError(code,reply)
        try:
            # Bounded DATA writes; never accumulate the whole MIME in memory.
            at_start=True
            pending=bytearray()
            while chunk:=message.readline(CHUNK):
                if at_start and chunk.startswith(b'.'):pending.extend(b'.')
                pending.extend(chunk)
                if len(pending)>=65536:
                    smtp.send(pending);pending.clear()
                at_start=chunk.endswith(b'\n')
            if pending:smtp.send(pending)
            if not at_start:smtp.send(b'\r\n')
            smtp.send(b'.\r\n')
            code,reply=smtp.getreply()
        except Exception:
            raise SubmissionUncertain() from None
        if code!=250:raise smtplib.SMTPDataError(code,reply)
        return {'smtp_code': code, 'smtp_reply': reply.decode('utf-8','replace')[:1000],
                'accepted_recipients': [recipient]}


def send_streamed(smtp, email, attachments):
    """Compatibility entry point for existing protocol/streaming tests."""
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / 'message.eml'
        sender, recipient = str(email['From']), str(email['To'])
        spool_message(path, email, attachments)
        return submit_spool(smtp, path, sender, recipient)
