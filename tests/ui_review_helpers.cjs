/* Run with node --test tests/ui_review_helpers.cjs. Synthetic DOM, not browser acceptance. */
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const html=fs.readFileSync(path.join(__dirname,'../procurement/static/index.html'),'utf8');
const script=html.match(/<script>([\s\S]*?)<\/script>/)[1];
const helpers=script.split('// BEGIN PROCUREMENT REVIEW HELPERS')[1].split('\n').slice(1).join('\n').split('// END PROCUREMENT REVIEW HELPERS')[0];
// Bind the shipped escaping function without executing page startup or installing its DOM selector.
const escSource=script.slice(script.indexOf('const esc='),script.indexOf('const money='));
function context(extra={}){
  const ctx=vm.createContext({documentType:value=>value||'Документ',
    humanStatus:value=>({draft:'Черновик',confirmed:'Подтверждён'})[value]||'Статус уточняется',...extra});
  vm.runInContext(escSource+'\n'+helpers,ctx);
  return ctx;
}
const entries=[
  {id:1,status:'draft',item_name:'Кабель <script>',quantity:2,unit:'м',unit_price:'100',currency:'RUB',vat_included:0,
   source_filename:'invoice<img>.xlsx',source_sheet:'Лист1',source_row:3,source_text:'<img onerror=alert(1)>'},
  {id:2,status:'confirmed',item_name:'Болт',unit_price:'200',currency:'KZT',vat_included:1,confirmed_by:'Reviewer',confirmed_at:'2026-09-04'},
  {id:3,status:'draft',item_name:'Кабель 2',unit_price:'101',currency:'RUB',vat_included:null}
];
test('complete shipped inline JavaScript parses',()=>{new vm.Script(script)});
for(const [value,label] of [[true,'С НДС'],[1,'С НДС'],[false,'Без НДС'],[0,'Без НДС'],[null,'Не указан'],['false','Не указан']]){
  test(`VAT ${JSON.stringify(value)} is explicit`,()=>assert.equal(context().vatBasis(value),label));
}
test('preview includes every row without preselecting or selecting confirmed rows',()=>{
  const output=context().importedPriceRows(entries,true);
  assert.equal((output.match(/<tr>/g)||[]).length,3);
  assert.equal((output.match(/name="batchEntry"/g)||[]).length,2);
  assert.ok(!output.includes(' checked'));
  assert.ok(!output.includes('value="2"'));
  assert.ok(output.includes('Reviewer')&&output.includes('2026-09-04'));
});
test('preview escapes item, source filename and full evidence',()=>{
  const output=context().importedPriceRows(entries,true);
  assert.ok(output.includes('&lt;script&gt;')&&output.includes('invoice&lt;img&gt;.xlsx'));
  assert.ok(output.includes('&lt;img onerror=alert(1)&gt;')&&!output.includes('<img'));
  assert.ok(output.includes('строка 3')&&output.includes('100 RUB'));
});
test('selection confirms only checked drafts and removes duplicate IDs',()=>{
  assert.equal(JSON.stringify(context().selectedImportIds(entries,['3','3'])),'[3]');
});
for(const values of [[],['2'],['999'],['1.2'],['NaN']]){
  test(`unsafe selection ${values} fails closed`,()=>assert.throws(()=>context().selectedImportIds(entries,values)));
}
for(const status of ['draft','confirmed','']){
  test(`archive filter ${status||'all'} uses API status`,()=>assert.equal(context().archiveEntriesPath(status),`/api/price-history-entries?status=${status}&limit=500`));
}
test('unsupported filter is refused',()=>assert.throws(()=>context().archiveEntriesPath('paid')));
test('outbox full body is untruncated text with escaped recipient and subject',()=>{
  const body='x'.repeat(31000)+'<END>';
  const output=context().messagePreview({channel:'email',recipient:'<a>',subject:'<script>',body});
  assert.ok(output.includes('x'.repeat(31000)+'&lt;END&gt;'));
  assert.ok(output.includes('&lt;a&gt;')&&output.includes('&lt;script&gt;')&&!output.includes('<script>'));
});
function receipt(){const hash='a'.repeat(64);return {mode:'sandbox',status:'simulated',external_send:false,message_id:1,channel:'email',payload_sha256:hash,receipt_id:`sandbox-email-${hash}`}}
test('valid receipt explicitly says simulation and not delivery',()=>{
  const ctx=context(),r=receipt();assert.ok(ctx.validSandboxReceipt(r,1));
  assert.ok(ctx.sandboxReceiptMarkup(r,1).includes('external_send=false'));
  assert.ok(ctx.sandboxReceiptMarkup(r,1).includes('не доставка адресату'));
});
for(const mutation of [{external_send:true},{message_id:2},{mode:'live'},{receipt_id:'<img>'},{channel:'other'}]){
  test(`invalid receipt ${Object.keys(mutation)[0]} does not claim success`,()=>{
    const ctx=context(),r={...receipt(),...mutation};assert.equal(ctx.validSandboxReceipt(r,1),false);
    assert.ok(ctx.sandboxReceiptMarkup(r,1).includes('не прошла проверку'));
  });
}
test('financial columns preserve source amounts and payment text; never normalize',()=>{
  const output=context().quoteFinancialCells({currency:'KZT',vat_included:false,subtotal:200,delivery_cost:17,total_cost:217,lead_days:4,payment_terms:'50% <аванс>'});
  for(const text of ['KZT','Без НДС','200','17','217','4 дн.','50% &lt;аванс&gt;'])assert.ok(output.includes(text));
  assert.equal((output.match(/<td/g)||[]).length,7);
});
test('missing VAT or payment is not fabricated',()=>{
  const output=context().quoteFinancialCells({});
  assert.ok(output.includes('Не указан')&&output.includes('Не указаны')&&!output.includes('RUB'));
});
test('actual batch handler submits selected row only, not all drafts',async()=>{
  const nodes={};for(const key of ['#modalTitle','#modalBody','#modalSubmit','#batchConfirmBy','#batchSelectionCount','#batchReject'])nodes[key]={value:'Synthetic reviewer'};
  nodes['#modal']={showModal(){},close(){}};
  const checkboxes=[{value:'1',checked:false},{value:'3',checked:true}];
  const calls=[];
  const ctx=context({$:s=>nodes[s],document:{querySelector:()=>null,querySelectorAll:s=>s.includes(':checked')?checkboxes.filter(c=>c.checked):checkboxes},
    api:async(url,options)=>{if(!options)return {filenames:['<file>.xlsx'],price_history_entries:entries};calls.push([url,JSON.parse(options.body)]);return {confirmed:1}},
    toast(){},loadArchive:async()=>{},renderArchive(){}});
  vm.runInContext(script.slice(script.indexOf('async function openBatch(id)'),script.indexOf('function updateDraftConfirmState')),ctx);
  await ctx.openBatch(4);
  assert.equal(nodes['#modalSubmit'].disabled,true);
  assert.ok(nodes['#modalBody'].innerHTML.includes('&lt;file&gt;.xlsx'));
  checkboxes[1].onchange();
  assert.equal(nodes['#modalSubmit'].disabled,false);
  await nodes['#modalSubmit'].onclick();
  assert.deepEqual(calls,[['/api/imports/4/confirm',{confirmed_by:'Synthetic reviewer',entry_ids:[3]}]]);
});
for(const allowed of [false,true])test(`explicit rejection requires confirmation ${allowed}`,async()=>{
  const nodes={};for(const key of ['#modalTitle','#modalBody','#modalSubmit','#batchConfirmBy','#batchSelectionCount','#batchReject'])nodes[key]={value:'Synthetic reviewer'};
  nodes['#modal']={showModal(){},close(){}};
  const checkbox={value:'3',checked:true};const calls=[];
  const ctx=context({$:s=>nodes[s],confirm:()=>allowed,
    document:{querySelector:()=>null,querySelectorAll:()=>[checkbox]},
    api:async(url,options)=>{if(!options)return {filenames:['synthetic.xlsx'],price_history_entries:entries};calls.push([url,JSON.parse(options.body)]);return {rejected:1}},
    toast(){},loadArchive:async()=>{},renderArchive(){}});
  vm.runInContext(script.slice(script.indexOf('async function openBatch(id)'),script.indexOf('function updateDraftConfirmState')),ctx);
  await ctx.openBatch(4);checkbox.onchange();await nodes['#batchReject'].onclick();
  assert.deepEqual(calls,allowed?[['/api/launch/imports/4/reject',{rejected_by:'Synthetic reviewer',entry_ids:[3]}]]:[]);
});
test('approval and simulation stay separate user actions',()=>{
  const approval=script.slice(script.indexOf('function approveMessage(id)'),script.indexOf('async function simulateMessage'));
  const simulation=script.slice(script.indexOf('async function simulateMessage'),script.indexOf('function renderComparison'));
  assert.ok(approval.includes('messagePreview(message)')&&approval.includes('messageReviewed'));
  assert.ok(approval.includes('/approve')&&!approval.includes('/simulate'));
  assert.ok(simulation.includes('confirm(')&&simulation.includes('/simulate')&&simulation.includes('validSandboxReceipt'));
});
