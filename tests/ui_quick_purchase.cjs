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
assert(staff.includes('<div id="quickPending"></div>'));
const pendingFunctions=['refreshQuickDrafts','openPendingQuickDraft','loadMoreQuickDrafts']
  .map(name=>staff.match(new RegExp(`async function ${name}\\([^]*?\\n\\}`))?.[0]);
assert(pendingFunctions.every(Boolean),'recoverable pending-draft functions');
async function pendingRecovery(){
  const older={preview_id:'older',filename:'first.xlsx',available:true};
  const latest={preview_id:'latest',filename:'second.xlsx',available:true};
  const draft={status:'needs_review',draft:{preview_id:'latest'}};
  const pending={intake:null,pending:[],nextBefore:null};
  const calls=[];
  const context={quickPurchase:pending,state:{view:'other'},renderLots:()=>{},
    renderPendingQuickDrafts:()=>{},toast:()=>{},Number,Set,encodeURIComponent,
    $:()=>({scrollIntoView:()=>{}}),
    api:async url=>{
      calls.push(url);
      if(url==='/api/procurement/quick-draft')return {...draft,pending_drafts:[latest],next_before:10};
      if(url==='/api/procurement/quick-draft?before=10')
        return {pending_drafts:[older],next_before:null};
      if(url==='/api/procurement/quick-draft/older')
        return {status:'needs_review',draft:{preview_id:'older'}};
      throw new Error(`unexpected URL ${url}`);
    }};
  const source=pendingFunctions.join('\n');
  const refresh=vm.runInNewContext(`${source};refreshQuickDrafts`,context);
  const more=vm.runInNewContext(`${source};loadMoreQuickDrafts`,context);
  const open=vm.runInNewContext(`${source};openPendingQuickDraft`,context);
  await refresh();assert.equal(pending.intake.draft.preview_id,'latest');
  await more();assert.deepEqual(pending.pending.map(item=>item.preview_id),['latest','older']);
  await open('older');assert.equal(pending.intake.draft.preview_id,'older');
  assert.deepEqual(calls,[
    '/api/procurement/quick-draft','/api/procurement/quick-draft?before=10',
    '/api/procurement/quick-draft/older']);
}
const outcomeSource=flow.match(/function procurementMailOutcome[\s\S]*?\n\}/)?.[0];
assert(outcomeSource,'mail outcome classifier');
const classify=(messages,campaign)=>vm.runInNewContext(`${outcomeSource};procurementMailOutcome(messages,campaign)`,{messages,campaign});
const campaign={messages:[{id:1},{id:2}]};
assert.equal(classify([{id:1,delivery:{status:'sent'}},{id:2,delivery:{status:'unknown'}}],campaign).kind,'unknown');
assert.equal(classify([{id:1,delivery:{status:'sent'}},{id:2,delivery:{status:'failed'}}],campaign).kind,'failed');
assert.equal(classify([{id:1,delivery:{status:'sent'}},{id:2,delivery:{status:'sent'}}],campaign).kind,'sent');
assert.equal(classify([],campaign).kind,'unknown');
assert(flow.includes("outcome==='unknown'||outcome==='sent'?'':`<button"),'uncertain SMTP result cannot expose Retry');
const sendSource=flow.match(/async function sendProcurement\(\)\{[\s\S]*?\n\}/)?.[0];
assert(sendSource,'send workflow');
async function sendFailure(attempted){
  const purchasing={preview:{lot_id:7,snapshot_sha256:'s',preview_sha256:'p'},request:{supplier_ids:[2],item_ids:[3]},lastSendOutcome:null};
  let outboxReads=0,sendCalls=0;
  const button={disabled:false,isConnected:true};
  const status={textContent:''};
  const context={purchasing,state:{selectedLot:7},selectedRfqRequest:()=>purchasing.request,
    $:selector=>selector==='#procurementSend'?button:status,
    launchJson:async url=>{
      if(url.endsWith('/campaigns')){
        if(!attempted)throw new Error('Предпросмотр устарел');
        return {lot_id:7,messages:[{id:9}]};
      }
      sendCalls++;throw new Error('SMTP timeout');
    },
    api:async()=>{outboxReads++;return [{id:9,delivery:{status:'unknown'}}]},
    procurementMailOutcome:classify,toast:()=>{},loadAll:async()=>{},openLot:async()=>{},
    invalidateProcurement:()=>{},confirmedMailNotice:()=>{},JSON};
  await vm.runInNewContext(`${sendSource};sendProcurement()`,context);
  return {purchasing,status,outboxReads,sendCalls,button};
}
(async()=>{
  await pendingRecovery();
  const before=await sendFailure(false);
  assert.equal(before.purchasing.lastSendOutcome.kind,'failed');
  assert.equal(before.outboxReads,0);
  assert.equal(before.sendCalls,0);
  assert.match(before.status.textContent,/Не отправлено/);
  const during=await sendFailure(true);
  assert.equal(during.purchasing.lastSendOutcome.kind,'unknown');
  assert.equal(during.outboxReads,1);
  assert.equal(during.sendCalls,1);
  assert.match(during.status.textContent,/не подтверждён/);
  console.log('quick purchase UI: upload → saved draft → inline preview → one send action, honest failure PASS');
})().catch(error=>{console.error(error);process.exitCode=1});
