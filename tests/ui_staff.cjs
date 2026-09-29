'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync('procurement/static/staff-ui.js', 'utf8');
const context = {
  mailStatus: {}, state: {role:'staff', activity:[]},
  mailJournal: () => 'TECHNICAL AUDIT', badge: () => 'technical badge',
  esc: value => String(value ?? '').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;'),
  date: value => value,
};
vm.createContext(context);
vm.runInContext(source.slice(0, source.indexOf('const fullRenderLots')), context);

const accepted = {id:1, recipient:'buyer@example.test', status:'sent',
  delivery:{status:'sent', accepted_at:'2026-09-28T09:00:00Z', sent_copy_status:'saved',
    rfc_message_id:'<private@example.test>', smtp_code:250,
    attachments:[{filename:'ФБС.pdf',sha256:'private-sha'}], events:[{event:'mail_sent'}]}};
assert.equal(context.staffMailStatus(accepted), 'Отправлен');
assert.equal(context.staffMailStatus({status:'sent',delivery:{status:'sent'}}), 'Результат отправки не подтверждён');
const journal = context.mailJournal(accepted);
assert(journal.includes('ФБС.pdf'));
for (const secret of ['private-sha','private@example.test','mail_sent','SHA256','250'])
  assert(!journal.includes(secret), `staff journal leaked ${secret}`);
const failed = context.mailJournal({id:2,recipient:'buyer@example.test',status:'failed',
  delivery:{status:'failed',error:'SMTP connection timeout <secret>'}});
assert(failed.includes('Почтовый сервер не ответил вовремя.'));
assert(!failed.includes('SMTP connection'));
context.state.role = 'admin';
assert.equal(context.mailJournal(accepted), 'TECHNICAL AUDIT');
context.state.role = 'staff';
context.state.activity = [{label:'Поставщик изменён', name:'Тест <script>', view:'suppliers', target_id:5,
  created_at:'2026-09-28'}];
const activity = context.staffActivity();
assert(activity.includes('Поставщик изменён'));
assert(activity.includes('Тест &lt;script&gt;'));
assert(!activity.includes('<script>'));
assert.deepEqual(Array.from(context.staffLotCounts([
  {status:'draft'}, {status:'rfq_draft'}, {status:'rfq_sent'},
  {status:'rfq_sent'}, {status:'quotes_received'}, {status:'comparison'},
  {status:'awarded'}, {status:'ordered'}
]), row => [row.label, row.count]), [
  ['Черновики', 2], ['Запрос отправлен', 2],
  ['Получены цены', 2], ['Поставщик выбран', 2]
]);
console.log('staff UI: Russian statuses, server-confirmed send, safe activity and admin-only audit PASS');
