"""Every supplier reply is visible in its purchase; the supplier profile keeps foreign suppliers out."""
from datetime import date, timedelta
from decimal import Decimal
from email.message import EmailMessage

import pytest

from procurement.inbox_quotes import MessageLink, link_message, lot_correspondence, unlink_message
from procurement.models import LotCreate, LotItemCreate, ProjectCreate, SupplierCreate
from procurement.service import ConflictError
from test_inbox_quotes import RFQ_ID, purchase, receive, reply
from test_launch_http import http_boundary  # noqa: F401  (fixture)
from test_launch_workflow import workflow  # noqa: F401  (fixture)


def text_reply(body, sender='Завод <sales@zavod.test>', subject='Re: Заявка от ООО ДНЕПР', in_reply_to=None):
    mail = EmailMessage()
    mail['From'], mail['To'], mail['Subject'] = sender, 'snab@dnepr.test', subject
    mail['Message-ID'] = '<text-reply@zavod.test>'
    if in_reply_to:
        mail['In-Reply-To'] = in_reply_to
    mail.set_content(body)
    return mail.as_bytes()


def test_reply_to_our_request_is_in_the_purchase_with_its_text(workflow, tmp_path):
    db, service, launch = workflow
    lot, supplier = purchase(service)
    receive(workflow, tmp_path, text_reply('Уточните, нужна ли доставка?', sender='a@example.test', in_reply_to=RFQ_ID))
    view = lot_correspondence(service, lot['id'])
    assert [m['excerpt'] for m in view['linked']] == ['Уточните, нужна ли доставка?']
    assert view['linked'][0]['supplier'] == {'id': supplier['id'], 'name': supplier['name'], 'source': 'request'}
    assert view['linked'][0]['linked_by'] == 'request' and view['unsorted'] == []


def test_reply_to_mail_sent_by_hand_waits_for_a_person_and_is_then_sorted(workflow, tmp_path):
    db, service, launch = workflow
    lot, supplier = purchase(service)
    with db.connection() as conn:
        conn.execute('UPDATE suppliers SET email=? WHERE id=?', ('sales@zavod.test', supplier['id']))
    _, message = receive(workflow, tmp_path, text_reply('Не наш профиль, отказываемся.'))
    view = lot_correspondence(service, lot['id'])
    assert view['linked'] == [] and [m['id'] for m in view['unsorted']] == [message['id']]
    assert view['unsorted'][0]['supplier']['source'] == 'sender'  # a suggestion from the address only
    link_message(service, message['id'], MessageLink(lot_id=lot['id'], supplier_id=supplier['id'], kind='refusal'))
    view = lot_correspondence(service, lot['id'])
    assert view['unsorted'] == [] and view['linked'][0]['kind'] == 'refusal'
    assert view['linked'][0]['linked_by'] == 'person'
    assert db.one("SELECT details_json FROM audit_log WHERE action='inbox_message_linked'")
    unlink_message(service, message['id'])
    assert [m['id'] for m in lot_correspondence(service, lot['id'])['unsorted']] == [message['id']]


def test_a_reply_to_one_purchase_cannot_be_filed_under_another(workflow, tmp_path):
    db, service, launch = workflow
    lot, supplier = purchase(service)
    other, _ = purchase(service, rfq_id='<rfq-0002@dnepr.test>', email='b@example.test', title='Плиты ПБ')
    _, message = receive(workflow, tmp_path, text_reply('Вопрос по сроку', sender='a@example.test', in_reply_to=RFQ_ID))
    with pytest.raises(ConflictError):
        link_message(service, message['id'], MessageLink(lot_id=other['id'], kind='question'))
    assert lot_correspondence(service, other['id'])['unsorted'] == []  # it is sorted already, into its own purchase


def test_saved_quote_files_the_reply_and_unrelated_mail_is_not_offered(workflow, tmp_path):
    db, service, launch = workflow
    lot, supplier = purchase(service)
    inbox, message = receive(workflow, tmp_path, reply())
    with db.connection() as conn:
        conn.execute('INSERT INTO inbox_quotes VALUES (?,?,?,?,NULL,?,?)',
                     (message['attachments'][0]['id'], message['id'], lot['id'], supplier['id'], 'x', '2026-10-10'))
    newsletter = EmailMessage()
    newsletter['From'], newsletter['Subject'] = 'news@webmaster.test', 'Уведомления о важном'
    newsletter['Message-ID'] = '<n@webmaster.test>'
    newsletter.set_content('Новости сервиса')
    receive(workflow, tmp_path, newsletter.as_bytes(), uid=2)
    view = lot_correspondence(service, lot['id'])
    assert [m['id'] for m in view['linked']] == [message['id']]  # by the request's Message-ID
    assert view['unsorted'] == []  # a newsletter is not a supplier reply


def test_bounce_is_offered_so_a_lost_request_is_noticed(workflow, tmp_path):
    db, service, launch = workflow
    lot, _ = purchase(service)
    receive(workflow, tmp_path, text_reply('Сообщение не доставлено', sender='mailer-daemon@yandex.test',
                                           subject='Недоставленное сообщение'))
    unsorted = lot_correspondence(service, lot['id'])['unsorted']
    assert len(unsorted) == 1 and unsorted[0]['bounce'] is True


def lot_with(service, title, items, cluster='cluster_2'):
    project = service.create_project(ProjectCreate(name='ТЕСТ ' + title, region='Воронежская область', cluster=cluster,
                                                   delivery_address='Воронеж, тестовая 1'))
    return service.create_lot(LotCreate(project_id=project['id'], title=title, region='Воронежская область', cluster=cluster,
                                        delivery_address='Воронеж, тестовая 1',
                                        response_deadline=date.today() + timedelta(days=7),
                                        items=[LotItemCreate(name=n, quantity=Decimal('10'), unit='шт') for n in items]))


def supplier(service, name, categories, cluster='cluster_2'):
    return service.create_supplier(SupplierCreate(name=name, region='Воронежская область', email=f'{abs(hash(name))}@s.test',
                                                  categories=categories, cluster=cluster, verified=True))


def test_fbs_purchase_is_offered_to_concrete_suppliers_only(workflow):
    db, service, launch = workflow
    lot = lot_with(service, 'ФБС', ['ФБС 24.4.6', 'ФБС 12.4.6'])
    concrete = supplier(service, 'Завод ЖБИ', ['ЖБИ'])
    cathodic = supplier(service, 'СЭЗ', ['ЭХЗ'])
    unknown = supplier(service, 'ТехноПром', [])
    loaded = service.get_lot(lot['id'])
    assert loaded['category'] == '' and loaded['suggested_category'] == 'ЖБИ' == loaded['effective_category']
    matches = {m['id']: m for m in service.match_suppliers(lot['id'])}
    assert concrete['id'] in matches and cathodic['id'] not in matches
    assert matches[unknown['id']]['auto_select'] is False and matches[unknown['id']]['match_reasons']['uncategorized']
    assert list(matches)[0] == concrete['id']  # the profile's own suppliers first


def test_person_sets_the_profile_and_ambiguous_text_suggests_nothing(workflow):
    db, service, launch = workflow
    lot = lot_with(service, 'Материалы', ['ФБС 24.4.6', 'Анод протекторный'])
    supplier(service, 'Завод ЖБИ', ['ЖБИ'])
    cathodic = supplier(service, 'СЭЗ', ['ЭХЗ'])
    assert service.get_lot(lot['id'])['suggested_category'] == ''  # both profiles named: no guess
    assert service.set_lot_category(lot['id'], '  ЭХЗ ')['category'] == 'ЭХЗ'
    assert [m['id'] for m in service.match_suppliers(lot['id'])] == [cathodic['id']]
    assert db.one("SELECT action FROM audit_log WHERE action='lot_category_set'")


def test_correspondence_is_administrator_only_and_linking_needs_csrf(http_boundary, tmp_path):
    from procurement.incoming_mail import Inbox
    from procurement.launch_workflow import LaunchWorkflow
    from procurement.service import ProcurementService
    from test_sso_adapter import ALICE, headers, login
    client, authority, settings, db = http_boundary
    service = ProcurementService(db)
    lot, _ = purchase(service)
    path = tmp_path / 'in.eml'
    path.write_bytes(text_reply('Отказ'))
    mid = Inbox(service, LaunchWorkflow(service)).ingest('account', 1, 1, path)
    url, link = f'/api/procurement/lots/{lot["id"]}/correspondence', f'/api/procurement/inbox/messages/{mid}/link'
    assert client.get(url).status_code == 401
    alice = login(client, authority)
    assert client.get(url).status_code == 403  # mailbox content is for administrators
    authority.users[ALICE]['access_admin'] = True
    alice = login(client, authority)
    response = client.get(url)
    assert response.status_code == 200, response.text
    assert response.json()['unsorted'][0]['excerpt'] == 'Отказ'
    body = {'lot_id': lot['id'], 'kind': 'refusal'}
    assert client.post(link, json=body).status_code == 403  # no CSRF token
    assert client.post(link, json={**body, 'kind': 'spam'}, headers=headers(alice)).status_code == 422
    assert client.post(link, json=body, headers=headers(alice)).status_code == 200
    assert client.get(url).json()['linked'][0]['kind'] == 'refusal'
    assert client.delete(link, headers=headers(alice)).status_code == 200
    category = f'/api/procurement/lots/{lot["id"]}/category'
    assert client.put(category, json={'category': 'ЭХЗ'}).status_code == 403  # no CSRF token
    assert client.put(category, json={'category': 'ЭХЗ'}, headers=headers(alice)).json()['category'] == 'ЭХЗ'


def test_old_mail_of_the_purchase_stays_after_newer_mail_arrives(workflow, tmp_path):
    db, service, launch = workflow
    lot, supplier = purchase(service)
    _, message = receive(workflow, tmp_path, text_reply('Срок 3 недели', sender='a@example.test', in_reply_to=RFQ_ID))
    with db.connection() as conn:
        conn.execute("UPDATE inbox_messages SET received_at='2025-01-01T00:00:00+00:00'")  # older than the pool
    assert [m['id'] for m in lot_correspondence(service, lot['id'])['linked']] == [message['id']]


def test_a_quote_saved_elsewhere_owns_the_reply(workflow, tmp_path):
    db, service, launch = workflow
    lot, supplier = purchase(service)
    other, other_supplier = purchase(service, rfq_id='<rfq-0002@dnepr.test>', email='b@example.test', title='Песок')
    inbox, message = receive(workflow, tmp_path, reply())  # answers the request of `lot`
    from procurement.inbox_quotes import save_quote
    from test_inbox_quotes import form
    from test_incoming_mail import admin
    with admin():  # a person saved its quote in the other purchase
        save_quote(service, inbox, message['attachments'][0]['id'], form(other, other_supplier, {0: '7'}))
    assert lot_correspondence(service, lot['id'])['linked'] == []  # not shown in both purchases
    filed = lot_correspondence(service, other['id'])['linked']
    assert [m['id'] for m in filed] == [message['id']]
    assert filed[0]['supplier'] == {'id': other_supplier['id'], 'name': other_supplier['name'], 'source': 'quote'}
    with pytest.raises(ConflictError):
        link_message(service, message['id'], MessageLink(lot_id=lot['id'], kind='question'))


def test_purchase_replies_are_listed_newest_first(workflow, tmp_path):
    db, service, launch = workflow
    lot, supplier = purchase(service)
    ids = []
    for uid, day in ((1, '2026-10-01'), (2, '2026-10-05'), (3, '2026-10-03')):
        _, message = receive(workflow, tmp_path, text_reply(f'Ответ {day}', sender='a@example.test', in_reply_to=RFQ_ID), uid=uid)
        with db.connection() as conn:
            conn.execute('UPDATE inbox_messages SET received_at=? WHERE id=?', (day + 'T10:00:00+00:00', message['id']))
        ids.append(message['id'])
    assert [m['id'] for m in lot_correspondence(service, lot['id'])['linked']] == [ids[1], ids[2], ids[0]]


def test_umbrella_and_product_categories_are_one_profile(workflow):
    db, service, launch = workflow
    lot = lot_with(service, 'ФБС', ['ФБС 24.4.6'])
    umbrella = supplier(service, 'Завод ЖБИ', ['ЖБИ'])
    product = supplier(service, 'Блоки ФБС', ['ФБС'])
    cathodic = supplier(service, 'СЭЗ', ['ЭХЗ'])
    loaded = service.get_lot(lot['id'])
    assert loaded['profile_categories'] == ['ЖБИ', 'ФБС'] and loaded['suggested_category'] == 'ЖБИ / ФБС'
    matches = {m['id'] for m in service.match_suppliers(lot['id'])}
    assert {umbrella['id'], product['id']} <= matches and cathodic['id'] not in matches


def test_designation_in_the_specification_sets_the_profile(workflow):
    db, service, launch = workflow
    project = service.create_project(ProjectCreate(name='ТЕСТ спец', region='Воронежская область', cluster='cluster_2',
                                                   delivery_address='Воронеж'))
    lot = service.create_lot(LotCreate(project_id=project['id'], title='Блоки', region='Воронежская область',
        cluster='cluster_2', delivery_address='Воронеж', response_deadline=date.today() + timedelta(days=7),
        items=[LotItemCreate(name='Блок фундаментный', specification='ФБС 24.4.6', quantity=Decimal('5'), unit='шт')]))
    supplier(service, 'Завод ЖБИ', ['ЖБИ'])
    cathodic = supplier(service, 'СЭЗ', ['ЭХЗ'])
    assert service.get_lot(lot['id'])['profile_categories'] == ['ЖБИ']
    assert cathodic['id'] not in {m['id'] for m in service.match_suppliers(lot['id'])}


def test_profile_chosen_by_hand_takes_the_whole_family(workflow):
    db, service, launch = workflow
    lot = lot_with(service, 'Материалы', ['Материал по проекту'])
    product = supplier(service, 'Блоки ФБС', ['ФБС'])
    cathodic = supplier(service, 'СЭЗ', ['ЭХЗ'])
    service.set_lot_category(lot['id'], 'ЖБИ')
    matches = {m['id'] for m in service.match_suppliers(lot['id'])}
    assert product['id'] in matches and cathodic['id'] not in matches


def test_composite_category_names_inherit_the_family_hints(workflow):
    db, service, launch = workflow
    lot = lot_with(service, 'ФБС', ['ФБС 24.4.6'])
    concrete = supplier(service, 'Завод', ['ЖБИ изделия'])
    cathodic = supplier(service, 'СЭЗ', ['ЭХЗ'])
    assert service.get_lot(lot['id'])['profile_categories'] == ['ЖБИ изделия']
    matches = {m['id'] for m in service.match_suppliers(lot['id'])}
    assert concrete['id'] in matches and cathodic['id'] not in matches
