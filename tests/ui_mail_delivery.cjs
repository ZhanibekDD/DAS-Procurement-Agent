'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync('procurement/static/launch.js','utf8');
const start=source.indexOf('const mailStatus='),end=source.indexOf('async function retrySentCopy');
const ctx={esc:s=>String(s).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;')};
vm.createContext(ctx);vm.runInContext(source.slice(start,end),ctx);
for(const value of [{},{status:'sent'},{status:'failed',accepted_by_smtp:true},{status:'sending',accepted_by_smtp:false}])
  assert.throws(()=>ctx.confirmedMailNotice(value),/не подтвердил/);
const warning='SMTP принял письмо, но копия в “Отправленных” не сохранена';
assert.equal(ctx.confirmedMailNotice({status:'sent',accepted_by_smtp:true,warning}),warning);
assert(ctx.confirmedMailNotice({status:'sent',accepted_by_smtp:true}).includes('Доставка адресату пока не подтверждена'));
const markup=ctx.mailJournal({id:3,recipient:'recipient@example.test',attachments:[],delivery:{status:'unknown',
 error:'<script>test</script>',recipients:['recipient@example.test'],rfc_message_id:'<id@example.test>',
 events:[{created_at:'2026-09-28',event:'mail_send_failed',details:{smtp_code:451}}]}});
assert(!markup.includes('<script>'));assert(markup.includes('&lt;script>'));assert(markup.includes('Повтор')||markup.includes('повтор'));
assert(!markup.includes('onclick="sendLaunchMessage'));assert(markup.includes('451'));
assert(source.includes('SMTP-повтор')===false); // no implicit resend in copy UI
assert(source.includes('/sent-copy'));
console.log('mail UI: explicit SMTP acceptance, Sent-copy warning, escaped journal, no false success PASS');
