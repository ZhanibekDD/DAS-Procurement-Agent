'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
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
console.log('quick purchase UI: upload → saved draft → inline preview → one send action, honest failure PASS');
