'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync('procurement/static/procurement.js','utf8');
const noop=()=>{},nodes={};
const ctx={pages:{},state:{selectedLot:2,outbox:[{id:1,lot_id:3,body:'ПБ'},{id:2,lot_id:2,body:'ФБС'}]},
  showView:noop,renderOverview:noop,renderLots:noop,renderProjects:noop,openLot:noop,renderRfq:noop,renderDocuments:noop,renderPricebook:noop,showModalForm:noop,
  document:{querySelectorAll:()=>[],querySelector:()=>null},$:k=>nodes[k],esc:s=>String(s),console};
vm.createContext(ctx);vm.runInContext(source,ctx);
assert.deepEqual(JSON.parse(JSON.stringify(ctx.filteredProcurementMessages(2))),[{id:2,lot_id:2,body:'ФБС'}]);
assert.equal(ctx.procurementStage({status:'ordered'}),5);assert.equal(ctx.procurementStage({status:'rfq_sent'}),1);
for(const s of ['snapshot_sha256:p.snapshot_sha256','preview_sha256:p.preview_sha256','p.items.some(i=>i.lot_id!==lid)','epoch!==purchasing.epoch','lid!==state.selectedLot',"fd.set('expected_sha256',preview.sha256)",'data-price-remove','Подтверждаю корректные строки'])assert(source.includes(s),s);
assert(!source.includes('showPdfPricePreview({...${JSON.stringify'));
const index=fs.readFileSync('procurement/static/index.html','utf8');
assert(!index.includes('data-view="rfq"'));assert(index.includes('data-view="lots"'));assert(index.includes('/assets/procurement.js'));
console.log('procurement UI: lot isolation, source hashes, race invalidation, stages, escaped PDF review PASS');
