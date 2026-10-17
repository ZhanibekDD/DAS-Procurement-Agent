'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const staff=fs.readFileSync('procurement/static/staff-ui.js','utf8');
const flow=fs.readFileSync('procurement/static/procurement.js','utf8');
const html=fs.readFileSync('procurement/static/index.html','utf8');
const launch=fs.readFileSync('procurement/static/launch.js','utf8');
const grab=(source,name)=>{const start=source.indexOf(`function ${name}(`);assert(start>=0,name);
  let depth=0;for(let i=source.indexOf('{',start);i<source.length;i++){
    if(source[i]==='{')depth++;else if(source[i]==='}'&&--depth===0)return source.slice(start,i+1)}
  throw new Error(`unbalanced ${name}`)};
const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

// archive table: rows open and restore by id, titles are escaped, empty archive explained
const context={esc,badge:status=>`<span class="badge">${status}</span>`,Number,Date};
const table=vm.runInNewContext(`${grab(staff,'lotArchiveDate')}\n${grab(staff,'archivedLotsTable')};archivedLotsTable`,context);
assert(table([]).includes('Архив пуст'));
const markup=table([{id:5,title:'<script>x</script> ЭХЗ',project_name:'Газпромнефть',region:'ЯНАО',project_id:2,
  archived_at:'2026-10-10T08:15:00+00:00',status:'draft'}]);
assert(markup.includes('restoreLot(5)')&&markup.includes('openLot(5)'));
assert(markup.includes('&lt;script&gt;')&&!markup.includes('<script>x'));
assert(markup.includes('data-project="2"'));
assert(/\d{2}\.\d{2}\.2026/.test(markup));
const date=vm.runInNewContext(`${grab(staff,'lotArchiveDate')};lotArchiveDate`,context);
assert.equal(date(''),'—');assert.equal(date('nonsense'),'—');

// the status filter gets the archive option; the purchase screen gets the archive/restore controls
assert(staff.includes('<option value="archived">Архив закупок</option>'));
assert(staff.includes("api('/api/lots?archived=only')"));
assert(staff.includes('`/api/lots/${id}/archive`')&&staff.includes('`/api/lots/${id}/restore`'));
assert(staff.includes('Запросы поставщикам из неё не отправляются'));

// supplier cluster: the create form offers it, the create call sends it, the edit modal reuses it
assert(html.includes('<select id="sCluster">${clusterOptions()}</select>'));
assert(html.includes("cluster:$('#sCluster')?.value||''"));
assert(launch.includes("$('#sCluster').value=s.cluster||''"));
const options=vm.runInNewContext(`const CLUSTER_LABELS={cluster_1:'Кластер 1 — Урал',cluster_2:'Кластер 2 — Юг'};${grab(html,'clusterOptions')};clusterOptions`,{esc});
assert(options('cluster_1').includes('<option value="cluster_1" selected>'));
assert(options('').includes('<option value="" selected>Определить по региону'));

// a purchase without matching suppliers names the unassigned ones that fit by category
const hintContext={esc,state:{suppliers:[
  {name:'ЭХЗ Саратов',cluster:'',active:true,categories:['ЭХЗ']},
  {name:'Окна',cluster:'',active:true,categories:['окна']},
  {name:'Удалён',cluster:'',active:false,categories:['ЭХЗ']},
  {name:'Уже в кластере',cluster:'cluster_1',active:true,categories:['ЭХЗ']}]},
  clusterLabel:c=>c==='cluster_1'?'Кластер 1 — Урал':'кластер не назначен'};
const hint=vm.runInNewContext(`${grab(flow,'procurementClusterHint')};procurementClusterHint`,hintContext);
const ehz={title:'ЭХЗ',cluster:'cluster_1',items:[{name:'Комплект ЭХЗ'}]};
const text=hint(ehz,false);
assert(text.includes('Подходят по категории, но без кластера: 1 — ЭХЗ Саратов')&&text.includes('«Кластер 1»'));
assert(!text.includes('Окна')&&!text.includes('Удалён'));
assert(hint(ehz,true).includes('Ещё подходят по категории'));  // shown next to the matched suppliers too
const sand={title:'Песок',cluster:'cluster_1',items:[{name:'Песок'}]};
assert(hint(sand,false).includes('Добавьте поставщика с кластером «Кластер 1»'));
assert.equal(hint(sand,true),'');
assert(flow.includes("||'<p>Нет подходящих поставщиков этого кластера и профиля.</p>'}${procurementClusterHint(lot,state.matches.length>0)}"));
console.log('ui_lot_archive ok');
