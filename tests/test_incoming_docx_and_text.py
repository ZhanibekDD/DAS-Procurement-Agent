"""Word quotes, the supplier's reply text and a one-time re-read of mail stored by an older reader."""
import io
import json
import zipfile
from email.message import EmailMessage

from procurement import incoming_mail
from procurement.incoming_mail import Inbox, poll, reply_excerpt, reread_pending
from test_incoming_mail import Mailbox, mailbox_env  # noqa: F401  (fixture)
from test_launch_workflow import workflow  # noqa: F401  (fixture)

W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'


def docx(rows, intro='Коммерческое предложение'):
    cell = lambda text: f'<w:tc><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:tc>'
    table = '<w:tbl>' + ''.join('<w:tr>' + ''.join(cell(c) for c in row) + '</w:tr>' for row in rows) + '</w:tbl>'
    document = (f'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="{W}"><w:body>'
                f'<w:p><w:r><w:t>{intro}</w:t></w:r></w:p>{table}</w:body></w:document>')
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr('[Content_Types].xml', '<Types/>')
        archive.writestr('word/document.xml', document)
    return buffer.getvalue()


QUOTE = docx([['№', 'Наименование продукции', 'Ед. изм.', 'Кол-во', 'Цена, руб. с НДС', 'Сумма, руб.'],
              ['1', 'ФБС 24.4.6', 'шт', '218', '5 120,50', '1 116 269'],
              ['2', 'ФБС 12.4.6', 'шт', '95', '2 640', '250 800']])


def mail(body='Добрый день! КП во вложении.', attachments=(), html=None, subject='Re: Заявка от ООО ДНЕПР'):
    message = EmailMessage()
    message['From'], message['To'], message['Subject'] = 'Завод <sales@zavod.test>', 'snab@dnepr.test', subject
    message['Message-ID'] = '<reply-1@zavod.test>'
    message.set_content(body)
    if html:
        message.add_alternative(html, subtype='html')
    for raw, maintype, subtype, filename in attachments:
        message.add_attachment(raw, maintype=maintype, subtype=subtype, filename=filename)
    return message.as_bytes()


WORD = (QUOTE, 'application', 'vnd.openxmlformats-officedocument.wordprocessingml.document', 'КП Днепр.docx')
LOGO = (b'GIF89a' + b'\x00' * 40, 'image', 'gif', 'logo.gif')


def receive(workflow, tmp_path, raw, uid=1):
    db, service, launch = workflow
    inbox = Inbox(service, launch)
    path = tmp_path / f'{uid}.eml'
    path.write_bytes(raw)
    mid = inbox.ingest('account', 1, uid, path)
    return inbox, mid


def test_word_quote_table_becomes_a_reviewable_draft(workflow, tmp_path):
    db, *_ = workflow
    inbox, mid = receive(workflow, tmp_path, mail(attachments=[WORD, LOGO]))
    message = db.one('SELECT status, error FROM inbox_messages WHERE id=?', (mid,))
    assert message['status'] == 'review' and message['error'] == ''  # the logo is skipped without a warning
    attachment = db.one('SELECT id, filename FROM inbox_attachments WHERE message_id=?', (mid,))
    draft = inbox.detail(attachment['id'])
    rows = [(r['item_name'], r['unit'], r['unit_price'], r['vat']) for r in draft['rows']]
    assert rows == [('ФБС 24.4.6', 'шт', '5 120,50', 'с НДС'), ('ФБС 12.4.6', 'шт', '2 640', 'с НДС')]
    assert draft['requires_review'] is True
    assert inbox.path(inbox.attachment(attachment['id'])).read_bytes() == QUOTE


def test_unknown_formats_are_still_reported(workflow, tmp_path):
    db, *_ = workflow
    _, mid = receive(workflow, tmp_path, mail(attachments=[(b'{\\rtf1}', 'application', 'rtf', 'kp.rtf')]))
    message = db.one('SELECT status, error FROM inbox_messages WHERE id=?', (mid,))
    assert message['status'] == 'no_prices' and 'DOCX' in message['error']


def test_reply_text_is_kept_without_the_quoted_request(workflow, tmp_path):
    db, *_ = workflow
    body = ('Добрый день!\nБлоки ФБС 24.4.6 в наличии нет, срок изготовления 3 недели.\nУточните, нужна ли доставка?\n\n'
            'Пн, 12 окт. 2026 г. в 10:00, ООО ДНЕПР <snab@dnepr.test>:\n> Просим направить КП\n> ФБС 24.4.6 — 218 шт')
    _, mid = receive(workflow, tmp_path, mail(body=body))
    excerpt = db.one('SELECT excerpt FROM inbox_texts WHERE message_id=?', (mid,))['excerpt']
    assert 'срок изготовления 3 недели' in excerpt and 'нужна ли доставка' in excerpt
    assert 'Просим направить КП' not in excerpt
    listing = Inbox(*workflow[1:]).listing()['messages'][0]
    assert listing['excerpt'] == excerpt


def test_html_reply_drops_blockquote_history_and_styles():
    html = ('<html><head><style>p{color:red}</style></head><body><div>Отказываемся, не наш профиль.</div>'
            '<blockquote><p>Запрос КП: ЭХЗ — ответ до 2026-10-21</p></blockquote></body></html>')
    message = EmailMessage()
    message['From'], message['Subject'] = 'a@b.test', 'Re: Запрос КП'
    message.set_content(html, subtype='html')
    assert reply_excerpt(message) == 'Отказываемся, не наш профиль.'


def test_forward_without_comment_still_shows_text():
    message = EmailMessage()
    message['From'], message['Subject'] = 'a@b.test', 'Fwd: Запрос КП'
    message.set_content('-------- Пересылаемое сообщение --------\nОт: ООО ДНЕПР\nЗапрос КП по ЭХЗ')
    assert 'Запрос КП по ЭХЗ' in reply_excerpt(message)


def test_mail_stored_by_the_old_reader_is_reread_once_from_the_mailbox(workflow, tmp_path, mailbox_env, monkeypatch):
    db, service, launch = workflow
    inbox = Inbox(service, launch)
    raw = mail(attachments=[WORD])
    monkeypatch.setattr(incoming_mail, 'SUPPORTED', {'.pdf', '.xlsx', '.csv'})  # the reader before this change
    box = Mailbox({1: raw})
    assert poll(inbox, lambda: box)['caught_up'] is True
    mid = db.one('SELECT id FROM inbox_messages')['id']
    with db.connection() as conn:  # the old reader stored neither texts nor upgrade marks
        conn.execute('DELETE FROM inbox_texts')
        conn.execute('DELETE FROM inbox_upgrades')
    assert db.one('SELECT status FROM inbox_messages')['status'] == 'no_prices'
    monkeypatch.setattr(incoming_mail, 'SUPPORTED', {'.pdf', '.xlsx', '.csv', '.docx'})
    assert reread_pending(inbox, lambda: box) == {'status': 'ok', 'reread': 1}
    assert db.one('SELECT status, error FROM inbox_messages')['status'] == 'review'
    assert len(db.all('SELECT id FROM inbox_attachments')) == 1
    assert db.one('SELECT excerpt FROM inbox_texts WHERE message_id=?', (mid,))['excerpt'].startswith('Добрый день')
    assert db.one('SELECT result FROM inbox_upgrades WHERE message_id=?', (mid,))['result'] == 'attachments:1'
    assert reread_pending(inbox, lambda: box) == {'status': 'ok', 'reread': 0}  # once only
    assert len(db.all('SELECT id FROM inbox_attachments')) == 1
    assert all('PEEK' in fields or fields == '(UID RFC822.SIZE)' or fields.startswith('UID ') for _, fields in box.commands)


def test_reread_never_uses_a_different_mailbox_copy(workflow, tmp_path, mailbox_env, monkeypatch):
    db, service, launch = workflow
    inbox = Inbox(service, launch)
    monkeypatch.setattr(incoming_mail, 'SUPPORTED', {'.pdf', '.xlsx', '.csv'})
    poll(inbox, lambda: Mailbox({1: mail(attachments=[WORD])}))
    with db.connection() as conn:
        conn.execute('DELETE FROM inbox_upgrades')  # stored by the old reader
    monkeypatch.setattr(incoming_mail, 'SUPPORTED', {'.pdf', '.xlsx', '.csv', '.docx'})
    changed = Mailbox({1: mail(body='Другое письмо под тем же UID', attachments=[WORD])})
    assert reread_pending(inbox, lambda: changed)['reread'] == 1
    assert db.one('SELECT result FROM inbox_upgrades')['result'] == 'source_changed'
    assert not db.all('SELECT id FROM inbox_attachments')
    assert db.one('SELECT status FROM inbox_messages')['status'] == 'no_prices'


def test_reread_failure_changes_nothing(workflow, tmp_path, mailbox_env, monkeypatch):
    db, service, launch = workflow
    inbox = Inbox(service, launch)
    monkeypatch.setattr(incoming_mail, 'SUPPORTED', {'.pdf', '.xlsx', '.csv'})
    poll(inbox, lambda: Mailbox({1: mail(attachments=[WORD])}))
    with db.connection() as conn:
        conn.execute('DELETE FROM inbox_upgrades')  # stored by the old reader
    def offline():
        raise OSError('PASSWORD transport')
    result = reread_pending(inbox, offline)
    assert result['status'] == 'error' and 'PASSWORD' not in json.dumps(result)
    assert not db.all('SELECT * FROM inbox_upgrades')


def test_reply_that_is_only_our_quoted_request_has_no_text():
    message = EmailMessage()
    message['From'], message['Subject'] = 'a@b.test', 'Re: Запрос КП: ФБС'
    message.set_content('> Просим направить КП\n> ФБС 24.4.6 — 218 шт')
    assert reply_excerpt(message) == ''
    html = EmailMessage()
    html['From'], html['Subject'] = 'a@b.test', 'Re: Запрос КП: ФБС'
    html.set_content('<blockquote>Просим направить КП</blockquote>', subtype='html')
    assert reply_excerpt(html) == ''


def test_new_mail_is_not_downloaded_again_by_the_reread(workflow, tmp_path, mailbox_env):
    db, service, launch = workflow
    inbox = Inbox(service, launch)
    box = Mailbox({1: mail(attachments=[WORD])})
    poll(inbox, lambda: box)
    assert db.one('SELECT result FROM inbox_upgrades')['result'] == 'ingested'
    downloads = len(box.commands)
    assert reread_pending(inbox, lambda: box) == {'status': 'ok', 'reread': 0}
    assert len(box.commands) == downloads  # no second IMAP fetch
