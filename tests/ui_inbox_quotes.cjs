'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync('procurement/static/procurement.js','utf8');
const noop=()=>{},nodes={},toasts=[],posts=[];let modal=null,vat=null,prices=[];
const el=(extra={})=>({innerHTML:'',textContent:'',value:'',checked:false,querySelectorAll:()=>[],setAttribute(k,v){this[k]=v},focus:noop,...extra});
const ctx={pages:{},state:{role:'admin',lots:[],suppliers:[]},showView:noop,renderOverview:noop,renderLots:noop,
 renderProjects:noop,openLot:noop,renderRfq:noop,renderDocuments:noop,renderPricebook:noop,showModalForm:noop,console,
 document:{querySelectorAll:q=>q==='[data-quote-item]'?prices:[],querySelector:q=>q==='input[name="inboxQuoteVat"]:checked'?vat:null},
 $:k=>nodes[k],toast:(m,e=false)=>toasts.push([m,e]),
 launchModal:(title,body,label)=>{modal={title,body,label}},launchJson:async(path,method,data)=>{posts.push({path,method,data});return {quote_id:7,lot_title:'Фундаментные <блоки>',items:data.items.length}},
 esc:s=>String(s).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;')};
vm.createContext(ctx);vm.runInContext(source,ctx);
(async()=>{
 const inbox=el();nodes['#mailPriceInbox']=inbox;
 const rfq={lot_id:3,supplier_id:9,lot_title:'Фундаментные <блоки>',supplier_name:'ООО <Бетон>',archived:false,sender_matches:false};
 ctx.api=async()=>({connections:[],next_before:null,messages:[
  {id:'m1',subject:'Re: Запрос',sender:'a@example.test',received_at:'2026-10-10T10:00:00Z',status:'review',error:'',rfq,
   attachments:[{id:'a1',filename:'kp.pdf',applied:0,quote:null}]},
  {id:'m2',subject:'КП',sender:'b@example.test',received_at:'2026-10-10T11:00:00Z',status:'review',error:'',rfq:null,
   attachments:[{id:'a2',filename:'kp2.xlsx',applied:0,quote:{quote_id:5,lot_title:'Песок'}}]}]});
 await ctx.loadIncomingPrices();
 const html=inbox.innerHTML;
 assert(html.includes('Ответ на запрос по закупке «Фундаментные &lt;блоки&gt;» · поставщик ООО &lt;Бетон&gt;'));
 assert(!html.includes('<блоки>')&&!html.includes('<Бетон>'));
 assert(html.includes('не с адреса, на который отправлен запрос'));
 assert(html.includes('data-inbox-quote="a1" data-inbox-message="m1">КП в закупку'));
 assert(html.includes('КП в закупке «Песок»')&&html.includes('КП сохранено в закупке')&&!html.includes('data-inbox-quote="a2"'));

 // the reply's purchase and supplier are preselected; only same-cluster active suppliers are offered
 ctx.state.lots=[{id:3,title:'Фундаментные <блоки>',project_name:'ЭХЗ',cluster:'cluster_1'},{id:4,title:'Архив',archived_at:'2026-10-01',cluster:'cluster_1'}];
 ctx.state.suppliers=[{id:9,name:'ООО <Бетон>',cluster:'cluster_1',active:true,tax_id:'8602'},{id:10,name:'Юг',cluster:'cluster_2',active:true},{id:11,name:'Удалён',cluster:'cluster_1',active:false}];
 for(const k of ['#inboxQuoteSupplier','#inboxQuoteItems','#modalSubmit','#inboxQuoteDelivery','#inboxQuoteLead','#inboxQuotePayment','#inboxQuotePriceDate','#inboxQuoteValid','#inboxQuoteVerified'])nodes[k]=el();
 nodes['#inboxQuoteLot']=el({value:'3'});nodes['#modal']={close(){this.closed=true}};nodes['#inboxQuoteDelivery'].value='0';
 ctx.api=async path=>{assert.equal(path,'/api/procurement/inbox/attachments/a1/quote-draft?lot_id=3');return {currency:'RUB',foreign_currencies:[],document_vat:['с НДС'],errors:[],saved:null,
  items:[{lot_item_id:21,name:'ФБС <24.4.6>',quantity:'10',unit:'шт',suggested_price:'5120.50',source_row:'ФБС 24.4.6',candidates:1,unit_differs:false},
         {lot_item_id:22,name:'Кабель',quantity:'100',unit:'м',suggested_price:'',source_row:'Кабель ВВГ',source_unit:'км',candidates:1,unit_differs:true}]}};
 await ctx.openInboxQuote('a1','m1');await new Promise(r=>setImmediate(r));
 assert.equal(modal.title,'КП в закупку');assert(modal.body.includes('<option value="3" selected>Фундаментные &lt;блоки&gt; · ЭХЗ'));
 assert(!modal.body.includes('Архив'));assert(modal.body.includes('/api/procurement/inbox/attachments/a1/original'));
 assert(modal.body.includes('Адрес отправителя отличается'));
 const supplierOptions=nodes['#inboxQuoteSupplier'].innerHTML;
 assert(supplierOptions.includes('<option value="9" selected>ООО &lt;Бетон&gt; · ИНН 8602'));
 assert(!supplierOptions.includes('Юг')&&!supplierOptions.includes('Удалён'));
 const items=nodes['#inboxQuoteItems'].innerHTML;
 assert(items.includes('data-quote-item="21" inputmode="decimal" value="5120.50"')&&items.includes('ФБС &lt;24.4.6&gt;'));
 assert(items.includes('В КП единица «км» вместо «м»')&&items.includes('В документе про НДС: с НДС'));

 // nothing is saved until VAT, valid prices and the confirmation are given
 nodes['#inboxQuoteSupplier'].value='9';
 prices=[el({value:'5 120,50',dataset:{quoteItem:'21'}}),el({value:'',dataset:{quoteItem:'22'}})];
 await nodes['#modalSubmit'].onclick();assert.match(toasts.at(-1)[0],/НДС/);
 vat={value:'true'};prices[1].value='12abc';
 await nodes['#modalSubmit'].onclick();assert.match(toasts.at(-1)[0],/числом/);assert.equal(prices[1]['aria-invalid'],'true');
 prices[1].value='';
 await nodes['#modalSubmit'].onclick();assert.match(toasts.at(-1)[0],/Сверьте/);assert.equal(posts.length,0);
 nodes['#inboxQuoteVerified'].checked=true;nodes['#inboxQuoteLead'].value='5';nodes['#inboxQuotePayment'].value=' 50% аванс ';
 ctx.api=async()=>({connections:[],next_before:null,messages:[]});
 await nodes['#modalSubmit'].onclick();
 assert.equal(posts.length,1);assert.equal(posts[0].path,'/api/procurement/inbox/attachments/a1/quote');
 assert.deepEqual(JSON.parse(JSON.stringify(posts[0].data)),{lot_id:3,supplier_id:9,vat_included:true,confirmed_source:true,delivery_cost:'0',lead_days:5,
  payment_terms:'50% аванс',price_date:null,valid_until:null,items:[{lot_item_id:21,unit_price:'5120.50'}]});
 assert(nodes['#modal'].closed);assert.match(toasts.at(-1)[0],/КП сохранено в закупке «Фундаментные <блоки>»: позиций 1/);

 // an archived purchase is not offered for a quote
 modal=null;inboxMessages_m3();
 await ctx.openInboxQuote('a3','m3');assert.equal(modal,null);assert.match(toasts.at(-1)[0],/в архиве/);
 console.log('inbox quotes UI: reply link, escaped, preselected, explicit VAT and confirmation PASS');
 function inboxMessages_m3(){vm.runInContext("inboxMessages.m3={id:'m3',rfq:{lot_id:4,supplier_id:9,lot_title:'Архив',supplier_name:'X',archived:true,sender_matches:true},attachments:[{id:'a3',filename:'x.pdf'}]}",ctx)}
})().catch(e=>{console.error(e);process.exitCode=1});
