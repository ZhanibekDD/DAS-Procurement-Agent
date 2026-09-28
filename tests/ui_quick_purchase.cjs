'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const staff=fs.readFileSync('procurement/static/staff-ui.js','utf8');
const flow=fs.readFileSync('procurement/static/procurement.js','utf8');
for(const value of [
  "$('#quickFile').onchange=startQuickPurchase",
  "api('/api/procurement/quick-intake'",
  'await openLot(result.lot.id)',
  'renderQuickReview()',
  'Не отправлено:',
  'Повторить'
])assert(staff.includes(value)||flow.includes(value),value);
assert(flow.includes("if(document.querySelectorAll('[name=\"procurementSupplier\"]:checked').length)await previewProcurement()"));
assert(flow.includes("host.replaceChildren(...rfq.childNodes)"));
assert(flow.includes("confirmedMailNotice(r)"));
assert(!flow.includes('procurementConfirmed'));
assert(staff.includes('quickPurchase.recovered'));
const outcomeSource=flow.match(/function procurementMailOutcome[\s\S]*?\n\}/)?.[0];
assert(outcomeSource,'mail outcome classifier');
const classify=(messages,campaign)=>vm.runInNewContext(`${outcomeSource};procurementMailOutcome(messages,campaign)`,{messages,campaign});
const campaign={messages:[{id:1},{id:2}]};
assert.equal(classify([{id:1,delivery:{status:'sent'}},{id:2,delivery:{status:'unknown'}}],campaign).kind,'unknown');
assert.equal(classify([{id:1,delivery:{status:'sent'}},{id:2,delivery:{status:'failed'}}],campaign).kind,'failed');
assert.equal(classify([{id:1,delivery:{status:'sent'}},{id:2,delivery:{status:'sent'}}],campaign).kind,'sent');
assert.equal(classify([],campaign).kind,'unknown');
assert(flow.includes("outcome==='unknown'||outcome==='sent'?'':`<button"),'uncertain SMTP result cannot expose Retry');
console.log('quick purchase UI: upload → saved draft → inline preview → one send action, honest failure PASS');
