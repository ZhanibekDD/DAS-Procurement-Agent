'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync('procurement/static/procurement.js','utf8');
const noop=()=>{},nodes={};
const ctx={pages:{},state:{role:'staff'},showView:noop,renderOverview:noop,renderLots:noop,
 renderProjects:noop,openLot:noop,renderRfq:noop,renderDocuments:noop,renderPricebook:noop,showModalForm:noop,
 document:{querySelectorAll:()=>[],querySelector:()=>null},$:k=>nodes[k],
 esc:s=>String(s).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;'),console};
vm.createContext(ctx);vm.runInContext(source,ctx);
(async()=>{
 const target={innerHTML:'',querySelectorAll:()=>[]};nodes['#mailPriceInbox']=target;
 ctx.api=async()=>({connections:[],next_before:null,messages:[{id:'id',subject:'<script>attack()</script>',sender:'<img src=x>',received_at:'2026-09-30T10:00:00Z',status:'review',error:'',attachments:[{id:'abc',filename:'<script>.pdf',applied:0}]}]});
 await ctx.loadIncomingPrices();
 assert(!target.innerHTML.includes('<script>'));assert(!target.innerHTML.includes('<img src=x>'));
 assert(target.innerHTML.includes('&lt;script&gt;'));assert(target.innerHTML.includes('Требует проверки'));
 ctx.api=async()=>{throw new Error('Почта недоступна')};await ctx.loadIncomingPrices();assert.equal(target.textContent,'Почта недоступна');
 assert(source.includes("if(state.role!=='admin')return"));assert(source.includes('confirmed_source:true'));
 assert(!source.includes('setInterval('));
 console.log('incoming mail UI: escaped content, explicit review, no false import, admin-only PASS');
})().catch(e=>{console.error(e);process.exitCode=1});
