'use strict';
// Purchase page: every supplier reply with its text; unsorted replies are marked by a person, never guessed.
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync('procurement/static/procurement.js','utf8');
const noop=()=>{},nodes={},posts=[],toasts=[];
const box={innerHTML:'',isConnected:true,buttons:[],querySelectorAll(q){return this.buttons.filter(b=>b.matches===q)}};
const ctx={pages:{},state:{role:'admin',lots:[],suppliers:[],selectedLot:5},showView:noop,renderOverview:noop,renderLots:noop,
 renderProjects:noop,openLot:noop,renderRfq:noop,renderDocuments:noop,renderPricebook:noop,showModalForm:noop,console,
 document:{querySelectorAll:()=>[],querySelector:()=>null},$:k=>nodes[k],toast:(m,e=false)=>toasts.push([m,e]),
 launchModal:noop,launchJson:async(path,method,data)=>{posts.push({path,method,data});return {}},
 esc:s=>String(s).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;')};
vm.createContext(ctx);vm.runInContext(source,ctx);
(async()=>{
 nodes['#lotCorrespondence']=box;
 const linked={id:'m1',sender:'a@example.test',subject:'Re: Запрос КП',received_at:'2026-10-14T08:00:00Z',status:'no_prices',error:'',
  excerpt:'Уточните <срок> доставки',attachments:[],supplier:{id:9,name:'ООО <Бетон>',source:'request'},bounce:false,kind:'reply',linked_by:'request'};
 const unsorted={id:'m2',sender:'b@zavod.test',subject:'Re: Заявка от ООО ДНЕПР',received_at:'2026-10-15T10:00:00Z',status:'review',error:'',
  excerpt:'КП во вложении',attachments:[{id:'a2',filename:'КП.docx',quote:null}],supplier:{id:11,name:'Завод ЖБИ',source:'sender'},
  bounce:false,kind:'reply',linked_by:''};
 const bounce={...unsorted,id:'m3',subject:'Недоставленное сообщение',attachments:[],supplier:null,bounce:true,excerpt:''};
 ctx.api=async url=>{assert.equal(url,'/api/procurement/lots/5/correspondence');return {linked:[linked],unsorted:[unsorted,bounce]}};
 await ctx.loadLotCorrespondence(5);
 const html=box.innerHTML;
 assert(html.includes('ООО &lt;Бетон&gt;')&&!html.includes('<Бетон>')&&html.includes('Уточните &lt;срок&gt; доставки'));
 assert(html.includes('Ответ — не разобран'));
 assert(html.includes('Письма без привязки (2)'));
 assert(html.includes('Завод ЖБИ <small class="muted">(по адресу отправителя — проверьте)</small>'));
 assert(html.includes('data-corr-quote="a2" data-corr-message="m2">КП в эту закупку'));
 assert(html.includes('data-corr-link="m2" data-kind="refusal">Отказ'));
 assert(html.includes('Письмо не доставлено'));
 assert(!html.includes('data-corr-link="m1"'));  // a reply to our request is already in the purchase

 // marking sends the purchase, the suggested supplier and the kind; nothing else
 const button={matches:'[data-corr-link]',dataset:{corrLink:'m2',kind:'refusal'},disabled:false};
 box.buttons=[button];
 await ctx.loadLotCorrespondence(5);
 await button.onclick();
 assert.equal(JSON.stringify(posts.at(-1)),JSON.stringify({path:'/api/procurement/inbox/messages/m2/link',method:'POST',data:{lot_id:5,supplier_id:11,kind:'refusal'}}));

 await new Promise(r=>setTimeout(r,0));  // the list reloads after marking
 // a non-administrator does not see mailbox content
 ctx.state.role='staff';
 await ctx.loadLotCorrespondence(5);
 assert(box.innerHTML.includes('видит администратор'));

 // the supplier profile selector offers the known profiles and explains the suggestion
 const control=ctx.procurementProfileControl({category:'',suggested_category:'ЖБИ',known_categories:['ЖБИ','ЭХЗ']});
 assert(control.includes('По названию: ЖБИ')&&control.includes('<option value="ЭХЗ" >ЭХЗ</option>'));
 assert(control.includes('Поставщики другого профиля не показываются'));
 console.log('ui_lot_correspondence ok');
})().catch(e=>{console.error(e);process.exit(1)});
