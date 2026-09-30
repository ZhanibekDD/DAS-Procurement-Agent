"""TLS IMAP Sent archival, with bounded APPEND and Message-ID reconciliation.

SMTP acceptance is not delivery. Failed/uncertain APPEND never triggers SMTP.
No credentials or server exception strings are logged/returned.
"""
import hashlib
import imaplib
import os
import re
import socket
import ssl
import threading
import time
from email.parser import BytesHeaderParser
from pathlib import Path

from .upload_io import CHUNK

# A broken provider search index must not cause blind APPENDs or an unbounded
# mailbox download. Only Message-ID headers are read, never bodies/attachments.
MAX_SCAN_MESSAGES = 10000
MAX_HEADER_BYTES = 8192
SCAN_SECONDS = 120


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
    if typ == 'OK':
        return _search_uids(data)
    if typ not in {'NO', 'BAD'}:
        raise ValueError('Поиск копии письма не выполнен')
    # Some providers authenticate and FETCH correctly but reject HEADER SEARCH.
    # An incomplete or changing scan is not proof of absence: fail closed.
    return _scan_message_headers(client, message_id)


def _search_uids(data):
    if len(data) != 1 or (data[0] is not None and not isinstance(data[0], bytes)):
        raise ValueError('Неполный результат поиска IMAP')
    identifiers = (data[0] or b'').split()
    if (any(not re.fullmatch(rb'[1-9][0-9]{0,9}', uid) or int(uid) > 2**32 - 1 for uid in identifiers)
            or len(set(identifiers)) != len(identifiers)):
        raise ValueError('Некорректный результат поиска IMAP')
    return identifiers


def _scan_message_headers(client, message_id):
    """A wall-clock watchdog also interrupts trickling socket reads."""
    if SCAN_SECONDS <= 0:
        raise ValueError('Проверка копии письма превысила время ожидания')
    deadline = time.monotonic() + SCAN_SECONDS
    transport = client.sock
    previous_timeout = transport.gettimeout()
    expired = threading.Event()

    def expire():
        expired.set()
        try:
            # shutdown interrupts a blocked SSL/socket read. Closing imaplib's
            # buffered file here could instead wait for that read's lock.
            transport.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    watchdog = threading.Timer(SCAN_SECONDS, expire)
    watchdog.daemon = True
    transport.settimeout(min(previous_timeout, SCAN_SECONDS) if previous_timeout else SCAN_SECONDS)
    watchdog.start()
    try:
        result = _read_message_headers(client, message_id, deadline)
        if expired.is_set() or time.monotonic() >= deadline:
            raise ValueError('Проверка копии письма превысила время ожидания')
        return result
    finally:
        watchdog.cancel()
        watchdog.join()
        if not expired.is_set():
            transport.settimeout(previous_timeout)


def _read_message_headers(client, message_id, deadline):

    def all_uids():
        if time.monotonic() >= deadline:
            raise ValueError('Проверка копии письма превысила время ожидания')
        typ, data = client.uid('SEARCH', None, 'ALL')
        if typ != 'OK':
            raise ValueError('Поиск копии письма не выполнен')
        identifiers = _search_uids(data)
        if len(identifiers) > MAX_SCAN_MESSAGES:
            raise ValueError('Поиск IMAP недоступен; слишком много писем для безопасной проверки')
        return identifiers

    identifiers = all_uids()
    found = []
    for offset in range(0, len(identifiers), 32):
        if time.monotonic() >= deadline:
            raise ValueError('Проверка копии письма превысила время ожидания')
        batch = identifiers[offset:offset + 32]
        typ, data = client.uid('FETCH', b','.join(batch),
                               f'(UID BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)]<0.{MAX_HEADER_BYTES + 1}>)')
        if typ != 'OK':
            raise ValueError('Не удалось прочитать заголовки IMAP')
        seen = set()
        for part in data:
            if not isinstance(part, tuple):
                continue  # imaplib emits closing parentheses separately.
            metadata, header = part
            uid = re.search(rb'\bUID ([1-9][0-9]*)\b', metadata)
            if (not uid or uid[1] not in batch or uid[1] in seen
                    or not isinstance(header, bytes) or len(header) > MAX_HEADER_BYTES
                    or (header != b'\r\n' and not header.endswith(b'\r\n\r\n'))):
                raise ValueError('Неполные заголовки IMAP; копия не подтверждена')
            seen.add(uid[1])
            parsed = BytesHeaderParser().parsebytes(header)
            values = parsed.get_all('Message-ID', [])
            if parsed.defects or len(values) > 1:
                raise ValueError('Неоднозначный Message-ID; копия не подтверждена')
            if values and values[0].strip() == message_id:
                found.append(uid[1])
        if seen != set(batch):
            raise ValueError('Неполные заголовки IMAP; копия не подтверждена')
    if set(all_uids()) != set(identifiers):
        raise ValueError('Папка изменилась во время проверки; повторите проверку копии')
    return found


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
