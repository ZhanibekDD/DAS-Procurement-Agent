'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const noop=()=>{},nodes={'#catalogImportPreview':{innerHTML:''},'#pdfPriceAdd':{},'#modalSubmit':{},'#catalogRowErrors':{innerHTML:''}};
const notices=[];
const ctx={pages:{},state:{role:'admin'},showView:noop,renderOverview:noop,renderLots:noop,
 renderProjects:noop,openLot:noop,renderRfq:noop,renderDocuments:noop,renderPricebook:noop,showModalForm:noop,
 document:{querySelectorAll:()=>[],querySelector:()=>null},$:k=>nodes[k],toast:(...x)=>notices.push(x),
 esc:s=>String(s).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;'),console};
vm.createContext(ctx);vm.runInContext(fs.readFileSync('procurement/static/procurement.js','utf8'),ctx);
(async()=>{
 ctx.showPdfPricePreview({preview_id:'preview',rows:[{item_name:'ФБС 24.4.6',unit_price:'',review_warning:'Проверьте цену',source_page:1,source_row:5}],errors:[],review_lines:[{page:1,line:1,text:'<script>untrusted</script>'}]});
 const before=nodes['#catalogImportPreview'].innerHTML;assert(before.includes('Проверьте цену'));
 assert(before.includes('&lt;script&gt;'));assert(!before.includes('<script>'));
 assert(before.includes('Все строки распознанного документа'));
 let payload;
 ctx.launchJson=async(url,method,data)=>{payload=data;return {rows:[],errors:[{row:1,error:'Нужна цена'}]};};
 await nodes['#modalSubmit'].onclick();
 assert.equal(payload.rows[0].item_name,'ФБС 24.4.6');assert.equal(payload.rows[0].currency,'RUB');
 assert(!('review_warning' in payload.rows[0]));assert(!('source_page' in payload.rows[0]));assert(!('source_row' in payload.rows[0]));
 assert.equal(nodes['#catalogImportPreview'].innerHTML,before);assert(nodes['#catalogRowErrors'].innerHTML.includes('Строка 1: Нужна цена'));
 assert.equal(nodes['#modalSubmit'].textContent,'Проверить строки');assert.equal(nodes['#modalSubmit'].disabled,false);
 vm.runInContext("catalogPdfRows[0].unit_price='123.40'",ctx);
 let checked;ctx.showConfirmedCatalog=p=>checked=p;ctx.launchJson=async(url,method,data)=>({rows:data.rows,errors:[]});
 await nodes['#modalSubmit'].onclick();assert.equal(checked.rows[0].unit_price,'123.40');
 assert(notices.every(x=>!String(x[0]).includes('сохранён успешно')));
 console.log('PDF review: editable rows only, retained corrections, escaped transcript, no false import PASS');
})().catch(e=>{console.error(e);process.exitCode=1});
