"""Provider search failure must neither duplicate mail nor claim an absent copy."""
import pytest
from procurement import sent_mail

MID = '<' + 'a' * 32 + '@example.test>'
OTHER = '<' + 'b' * 32 + '@example.test>'


class Mailbox:
    def __init__(self, headers=None, fault=None):
        self.headers = headers if headers is not None else [MID, OTHER, None]
        self.fault = fault
        self.searches = 0
        self.fetches = []

    def _quote(self, value):return '"' + value + '"'

    def select(self, folder, readonly):
        assert readonly
        return 'OK', [str(len(self.headers)).encode()]

    def uid(self, command, *args):
        if command == 'SEARCH' and 'HEADER' in args:
            if self.fault == 'transport':raise OSError('connection lost')
            if self.fault == 'normal':return 'OK', [b'1']
            return 'NO', [b'Backend error']
        if command == 'SEARCH':
            assert args == (None, 'ALL')
            self.searches += 1
            if self.fault == 'all_failed':return 'NO', [b'unavailable']
            ids = [str(i+1).encode() for i in range(len(self.headers))]
            if self.fault == 'changed' and self.searches == 2:ids.append(b'999')
            if self.fault == 'invalid_uid':ids = [b'1:*']
            if self.fault == 'repeated_uid':ids = [b'1', b'1']
            return 'OK', [b' '.join(ids)]
        assert command == 'FETCH'
        self.fetches.append(args)
        assert args[1] == '(UID BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)]<0.8193>)'
        if self.fault == 'fetch_failed':return 'NO', [b'unavailable']
        data = []
        for uid in args[0].split(b','):
            value = self.headers[int(uid)-1]
            header = ('Message-ID: ' + value + '\r\n\r\n').encode() if value else b'\r\n'
            if self.fault == 'oversized':header = b'x' * 8193
            if self.fault == 'truncated':header = header[:-2]
            if self.fault == 'two_headers':header = ('Message-ID: '+MID+'\r\nMessage-ID: '+MID+'\r\n\r\n').encode()
            data += [(b'1 (UID '+uid+b' BODY[HEADER.FIELDS (MESSAGE-ID)]<0> {64}', header), b')']
        if self.fault == 'missing_uid':data = data[2:]
        if self.fault == 'duplicate_fetch':data += data[:2]
        return 'OK', data


def test_provider_header_index_failure_exact_bounded_read_only_match():
    mailbox = Mailbox([OTHER] * 33 + [MID, None])
    assert sent_mail.find_message(mailbox, b'Sent', MID) == [b'34']
    assert len(mailbox.fetches) == 2 and mailbox.searches == 2


@pytest.mark.parametrize('headers,expected', [([], []), ([OTHER, None], []), ([MID, MID], [b'1',b'2'])])
def test_provider_scan_preserves_absence_and_duplicate_detection(headers, expected):
    assert sent_mail.find_message(Mailbox(headers), b'Sent', MID) == expected


@pytest.mark.parametrize('fault', ['all_failed', 'invalid_uid', 'repeated_uid', 'fetch_failed',
    'changed', 'oversized', 'truncated', 'two_headers', 'missing_uid', 'duplicate_fetch'])
def test_provider_incomplete_scan_never_claims_absence(fault):
    with pytest.raises(ValueError):sent_mail.find_message(Mailbox(fault=fault), b'Sent', MID)


def test_provider_scan_size_and_time_limits_fail_closed(monkeypatch):
    mailbox = Mailbox()
    monkeypatch.setattr(sent_mail, 'MAX_SCAN_MESSAGES', 2)
    with pytest.raises(ValueError):sent_mail.find_message(mailbox, b'Sent', MID)
    assert not mailbox.fetches
    monkeypatch.setattr(sent_mail, 'SCAN_SECONDS', 0)
    with pytest.raises(ValueError):sent_mail.find_message(Mailbox(), b'Sent', MID)


@pytest.mark.parametrize('fault', ['normal', 'transport'])
def test_success_or_transport_failure_never_triggers_mailbox_scan(fault):
    mailbox = Mailbox(fault=fault)
    if fault == 'transport':
        with pytest.raises(OSError):sent_mail.find_message(mailbox, b'Sent', MID)
    else:assert sent_mail.find_message(mailbox, b'Sent', MID) == [b'1']
    assert mailbox.searches == 0 and not mailbox.fetches


@pytest.mark.parametrize('reconcile', [False, True])
def test_incomplete_scan_never_appends_even_during_reconciliation(tmp_path, monkeypatch, reconcile):
    path = tmp_path/'mail.eml';path.write_bytes(b'original immutable message')
    mailbox = Mailbox(fault='missing_uid')
    mailbox.logout = lambda:None
    monkeypatch.setattr(sent_mail, 'connect_imap', lambda:mailbox)
    monkeypatch.setattr(sent_mail, 'sent_folder', lambda c:b'Sent')
    monkeypatch.setattr(sent_mail, 'append_streamed', lambda *args:pytest.fail('unsafe APPEND'))
    expected = sent_mail.ArchiveUncertain if reconcile else ValueError
    with pytest.raises(expected):
        sent_mail.archive_sent(path, sent_mail.file_digest(path), MID, reconcile_only=reconcile)
