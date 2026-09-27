"""Lot-bound immutable RFQ snapshots; the browser never supplies message text."""
import json
import re
from decimal import Decimal

from .db import utcnow
from .sandbox import payload_sha256, message_fingerprint
from .service import ConflictError, NotFoundError
from .templates import render_template

SCHEMA = '''
CREATE TABLE IF NOT EXISTS rfq_snapshots (
 campaign_id INTEGER PRIMARY KEY REFERENCES campaigns(id), lot_id INTEGER NOT NULL REFERENCES lots(id),
 snapshot_json TEXT NOT NULL, snapshot_sha256 TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS rfq_message_snapshots (
 message_id INTEGER PRIMARY KEY REFERENCES outbox_messages(id), payload_sha256 TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS procurement_decisions (
 lot_id INTEGER PRIMARY KEY REFERENCES lots(id), quote_id INTEGER NOT NULL REFERENCES quotes(id),
 stage TEXT NOT NULL, actor TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS procurement_policy (
 id INTEGER PRIMARY KEY CHECK(id=1), amount_threshold TEXT, currency TEXT NOT NULL DEFAULT 'RUB',
 required_roles_json TEXT NOT NULL DEFAULT '[]', updated_at TEXT NOT NULL);
'''


def lot_snapshot(conn, lot_id, item_ids=None):
    lot = conn.execute('SELECT * FROM lots WHERE id=?', (lot_id,)).fetchone()
    if not lot:
        raise NotFoundError('Закупка не найдена')
    lot = dict(lot)
    lot.pop('status')
    items = [dict(r) for r in conn.execute('SELECT * FROM lot_items WHERE lot_id=? ORDER BY id', (lot_id,))]
    # Explicit requirement for this named FBS lot, not a guess from an invoice.
    if re.fullmatch(r'блоки\s+фбс', lot['title'].strip(), re.I):
        actual = sorted((re.sub(r'\s+', '', i['name']).upper(), Decimal(i['quantity']), i['unit'].rstrip('.')) for i in items)
        expected = sorted((n, Decimal(q), 'шт') for n,q in [('ФБС24.4.6',218),('ФБС12.4.6',95),('ФБС9.4.6',128)])
        if actual != expected:
            raise ConflictError('Состав ФБС не совпадает с подтверждённой спецификацией 218 / 95 / 128 шт; отправка заблокирована')
    selected = sorted(set(item_ids or [i['id'] for i in items]))
    if not selected or not set(selected).issubset({i['id'] for i in items}):
        raise ConflictError('Выбранные позиции не принадлежат выбранному ID лота')
    attachments = [dict(r) for r in conn.execute('''SELECT d.id AS document_id,d.filename,d.sha256,d.size_bytes
        FROM lot_attachments a JOIN source_documents d ON d.id=a.document_id WHERE a.lot_id=? ORDER BY d.id''', (lot_id,))]
    return {'lot':lot, 'items':[i for i in items if i['id'] in selected], 'attachments':attachments}


def bind_campaign(conn, campaign_id, snapshot):
    conn.execute('INSERT INTO rfq_snapshots VALUES (?,?,?,?,?)',
        (campaign_id,snapshot['lot']['id'],json.dumps(snapshot,ensure_ascii=False),payload_sha256(snapshot),utcnow()))
    for row in conn.execute('SELECT id FROM outbox_messages WHERE campaign_id=?', (campaign_id,)):
        message = dict(conn.execute('''SELECT m.*,c.lot_id,l.cluster AS lot_cluster,p.cluster AS project_cluster,
            s.cluster AS supplier_cluster FROM outbox_messages m JOIN campaigns c ON c.id=m.campaign_id
            JOIN lots l ON l.id=c.lot_id JOIN projects p ON p.id=l.project_id
            JOIN suppliers s ON s.id=m.supplier_id WHERE m.id=?''',(row['id'],)).fetchone())
        message['attachments'] = snapshot['attachments']
        conn.execute('INSERT INTO rfq_message_snapshots VALUES (?,?)',(row['id'],message_fingerprint(message)))


def validate_message(conn, message):
    row = conn.execute('SELECT * FROM rfq_snapshots WHERE campaign_id=?', (message['campaign_id'],)).fetchone()
    if not row:
        raise ConflictError('Старый запрос не связан с проверенным составом лота. Создайте новый предпросмотр; старые данные сохранены')
    snapshot = json.loads(row['snapshot_json'])
    validate_rendered_items(snapshot,message['body'])
    if row['lot_id'] != message['lot_id'] or payload_sha256(lot_snapshot(conn,row['lot_id'],[i['id'] for i in snapshot['items']])) != row['snapshot_sha256']:
        raise ConflictError('Лот или его позиции изменились после предпросмотра; отправка заблокирована')
    stored = conn.execute('SELECT payload_sha256 FROM rfq_message_snapshots WHERE message_id=?',(message['id'],)).fetchone()
    if not stored or stored[0] != message_fingerprint(message):
        raise ConflictError('Текст, адресат или вложения не совпадают с проверенным запросом; отправка заблокирована')


class ProcurementFlow:
    def __init__(self, service):
        self.service, self.db = service, service.db

    def preview(self, lot_id, data):
        with self.db.connection() as conn:
            snapshot = lot_snapshot(conn,lot_id,data.item_ids)
            project = self.service.get_project(snapshot['lot']['project_id'])
            cluster = self.service._confirmed_cluster(snapshot['lot']['cluster'],project['cluster'])
            template = conn.execute('SELECT * FROM templates WHERE code=?',(data.template_code,)).fetchone()
            if not template:
                raise NotFoundError('Шаблон не найден')
            messages = []
            for sid in dict.fromkeys(data.supplier_ids):
                supplier = self.service.get_supplier(sid)
                self.service._supplier_cluster(supplier,cluster)
                recipient = supplier[{'email':'email','telegram':'telegram','max':'max_contact'}[data.channel]]
                if not recipient:
                    raise ConflictError('У выбранного поставщика нет контакта выбранного канала')
                context = {'supplier_name':supplier['name'],'lot_title':snapshot['lot']['title'],
                    'project_name':project['name'],'region':snapshot['lot']['region'],
                    'delivery_address':snapshot['lot']['delivery_address'],
                    'desired_delivery_date':snapshot['lot']['desired_delivery_date'] or 'по согласованию',
                    'response_deadline':snapshot['lot']['response_deadline'],'items':items_text(snapshot)}
                body=render_template(template['body'],context)
                validate_rendered_items(snapshot,body)
                messages.append({'supplier_id':sid,'supplier_name':supplier['name'],'recipient':recipient,
                    'channel':data.channel,'subject':render_template(template['subject'],context),
                    'body':body,'attachments':snapshot['attachments']})
            result = {'lot_id':lot_id,'snapshot_sha256':payload_sha256(snapshot),'items':snapshot['items'],'messages':messages,
                'approval_required':self.approval_required(conn,lot_id,'staff')}
            result['preview_sha256'] = payload_sha256(result)
            return result

    def approval_required(self, conn, lot_id, role):
        policy = conn.execute('SELECT * FROM procurement_policy WHERE id=1').fetchone()
        if not policy:
            return False
        if role in json.loads(policy['required_roles_json']):
            return True
        if policy['amount_threshold'] is not None:
            # An unknown amount is not an invented zero. Require approval until quoted.
            quotes = self.service.list_quotes(lot_id)
            if not quotes:
                return True
            quantities={i['id']:Decimal(i['quantity']) for i in self.service.get_lot(lot_id)['items']}
            for quote in quotes:
                if quote['currency'] != policy['currency']:
                    return True
                if {i['lot_item_id'] for i in quote['items']} != set(quantities):
                    return True
                if any(not i['compliant'] or (i['offered_quantity'] is not None and
                       Decimal(i['offered_quantity'])<quantities[i['lot_item_id']]) for i in quote['items']):
                    return True
                amount = Decimal(quote['delivery_cost']) + sum(Decimal(i['unit_price'])*quantities[i['lot_item_id']] for i in quote['items'])
                if amount >= Decimal(policy['amount_threshold']):
                    return True
        return False


def items_text(snapshot):
    text = '\n'.join(f"- {i['name']}: {i['quantity']} {i['unit']}" +
        (f"; {i['specification']}" if i['specification'] else '') +
        (f"; срок {i['delivery_date']}" if i.get('delivery_date') else '') for i in snapshot['items'])
    requirements = json.loads(snapshot['lot'].get('rfq_requirements_json') or '{}')
    labels={'delivery_address_confirmation':'Подтверждение адреса доставки','coating':'Покрытие','color_ral':'Цвет RAL',
            'mesh_cell':'Ячейка сетки','rod_diameter':'Диаметр прутка','delivery_or_pickup':'Логистика'}
    values={'delivery':'доставка поставщиком','pickup':'самовывоз','supplier_choice':'указать оба варианта'}
    rows=[f'- {labels[k]}: {values.get(str(v),v)}' for k,v in requirements.items() if v and k in labels]
    return text + ('\n\nДополнительные требования:\n'+'\n'.join(rows) if rows else '')


def validate_rendered_items(snapshot,body):
    if re.fullmatch(r'блоки\s+фбс',snapshot['lot']['title'].strip(),re.I):
        if re.search(r'(?<!\w)ПБ(?!\w)',body,re.I) or any(
            f"{i['name']}: {i['quantity']} {i['unit']}" not in body for i in snapshot['items']):
            raise ConflictError('Текст запроса ФБС не соответствует выбранным позициям; отправка заблокирована')
