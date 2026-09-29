"""TLS IMAP Sent archival, with bounded APPEND and Message-ID reconciliation.

SMTP acceptance is not delivery. Failed/uncertain APPEND never triggers SMTP.
No credentials or server exception strings are logged/returned.
"""
import hashlib
import imaplib
import os
import re
import ssl
from pathlib import Path

from .upload_io import CHUNK


class ArchiveUncertain(Exception):
    """An APPEND may have been accepted. Only SEARCH is safe afterwards."""


def file_digest(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        while chunk := stream.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def secret(prefix):
    path = Path(os.environ[prefix + '_PASSWORD_FILE'])
    if path.stat().st_mode & 0o077:
        raise ValueError('Почтовый secret должен иметь права 0400/0600')
    return path.read_text().strip()


def connect_imap():
    host = os.getenv('PROCUREMENT_IMAP_HOST', '')
    if not host:
        raise ValueError('IMAP не настроен')
    mode = os.getenv('PROCUREMENT_IMAP_TLS', 'ssl')
    port = int(os.getenv('PROCUREMENT_IMAP_PORT', '993' if mode == 'ssl' else '143'))
    if mode == 'none' and host not in {'localhost', '127.0.0.1', '::1'}:
        raise ValueError('IMAP без TLS запрещён')
    if mode == 'ssl':
        client = imaplib.IMAP4_SSL(host, port, ssl_context=ssl.create_default_context(), timeout=30)
    elif mode in {'none', 'starttls'}:
        client = imaplib.IMAP4(host, port, timeout=30)
    else:
        raise ValueError('Неверный IMAP TLS режим')
    try:
        if mode == 'starttls':
            client.starttls(ssl_context=ssl.create_default_context())
        prefix = 'PROCUREMENT_IMAP' if os.getenv('PROCUREMENT_IMAP_PASSWORD_FILE') else 'PROCUREMENT_SMTP'
        client.login(os.getenv('PROCUREMENT_IMAP_USER') or os.environ['PROCUREMENT_SMTP_USER'], secret(prefix))
        return client
    except Exception:
        client.shutdown()
        raise


def sent_folder(client):
    configured = os.getenv('PROCUREMENT_IMAP_SENT_FOLDER', '')
    if configured:
        if any(c in configured for c in '\r\n\x00'):
            raise ValueError('Некорректная папка IMAP')
        # An administrator-supplied IMAP name (ASCII/modified UTF-7), never input from a browser.
        return client._quote(configured).encode('ascii')
    typ, folders = client.list()
    if typ != 'OK':
        raise ValueError('Не удалось найти папку Отправленные')
    matches = []
    for item in folders:
        if isinstance(item, bytes) and re.search(rb'\\Sent(?:\s|\))', item, re.I):
            match = re.match(rb'^\([^)]*\)\s+(?:"[^"]*"|NIL)\s+(.+)$', item)
            if match:
                matches.append(match.group(1))
    if len(matches) != 1:
        raise ValueError('Папка Отправленные не определена однозначно')
    return matches[0]


def find_message(client, folder, message_id):
    if not re.fullmatch(r'<[a-f0-9]{32}@[A-Za-z0-9.-]+>', message_id):
        raise ValueError('Некорректный Message-ID')
    typ, _ = client.select(folder, readonly=True)
    if typ != 'OK':
        raise ValueError('Папка Отправленные недоступна')
    typ, data = client.uid('SEARCH', None, 'HEADER', 'Message-ID', client._quote(message_id))
    if typ != 'OK':
        raise ValueError('Поиск копии письма не выполнен')
    return (data[0] or b'').split()


def append_streamed(client, folder, path):
    """imaplib.append builds a whole bytes literal; this bounded variant does not.

    Uses the same tagged-command/continuation machinery, tested on a real IMAP
    socket fixture and the configured provider. Synchronizing literal only.
    """
    tag = client._new_tag()
    command = tag + b' APPEND ' + folder + b' (\\Seen) {' + str(Path(path).stat().st_size).encode() + b'}\r\n'
    client.send(command)
    while client._get_response():
        if client.tagged_commands[tag]:
            typ, _ = client._command_complete('APPEND', tag)
            raise ValueError('IMAP отклонил сохранение копии')
    with Path(path).open('rb') as stream:
        while chunk := stream.read(CHUNK):
            client.send(chunk)
    client.send(b'\r\n')
    typ, _ = client._command_complete('APPEND', tag)
    if typ != 'OK':
        raise ValueError('IMAP отклонил сохранение копии')


def archive_sent(path, expected_sha, message_id, *, reconcile_only=False):
    if file_digest(path) != expected_sha:
        raise ValueError('SHA письма не совпадает; копия не сохранена')
    client = connect_imap()
    try:
        folder = sent_folder(client)
        try:
            found = find_message(client, folder, message_id)
        except Exception:
            if reconcile_only:
                raise ArchiveUncertain() from None
            raise
        if len(found) > 1:
            raise ValueError('Обнаружены несколько копий; нужна проверка')
        if found:
            return {'status': 'saved', 'uid': found[0].decode('ascii')}
        # If a previous APPEND response was lost, absence in SEARCH is not proof
        # that the server will not finish it later. No duplicate APPEND on retry.
        if reconcile_only:
            return {'status': 'unknown', 'uid': None}
        try:
            append_streamed(client, folder, path)
        except ValueError:
            raise  # an explicit IMAP NO/BAD means no append happened
        except Exception:
            raise ArchiveUncertain() from None
        try:
            found = find_message(client, folder, message_id)
        except Exception:
            raise ArchiveUncertain() from None
        if len(found) != 1:
            return {'status': 'unknown', 'uid': None}
        return {'status': 'saved', 'uid': found[0].decode('ascii')}
    finally:
        try:
            client.logout()
        except Exception:
            try:
                client.shutdown()
            except Exception:
                pass
