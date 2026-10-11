'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const html=fs.readFileSync('procurement/static/index.html','utf8');
const launch=fs.readFileSync('procurement/static/launch.js','utf8');
const flow=fs.readFileSync('procurement/static/procurement.js','utf8');

// create form: checkbox next to the cluster select, and the create call sends it
assert(html.includes('<label class="check-inline"><input type="checkbox" id="sBoth"> Работает в обоих кластерах</label>'));
assert(html.includes("both_clusters:!!$('#sBoth')?.checked"));
assert(html.includes('.check-inline{'));

// edit modal: the checkbox is added once, filled from the card and sent back
assert(launch.includes("if(!$('#sBoth'))$('#sCluster').insertAdjacentHTML('afterend'"));
assert(launch.includes("$('#sBoth').checked=!!s.both_clusters;"));
assert(launch.includes("both_clusters:$('#sBoth').checked"));

// supplier registry shows both clusters instead of the single label
assert(html.includes(`\${s.both_clusters?'<small class="muted">Кластеры 1 и 2</small>':s.cluster?`));

// «КП в закупку»: a both-cluster supplier is offered for a purchase of either confirmed cluster
const filter=(suppliers,lot)=>suppliers.filter(s=>s.active!==false&&(!lot||!lot.cluster||s.cluster===lot.cluster||(s.both_clusters&&['cluster_1','cluster_2'].includes(lot.cluster))));
assert(flow.includes("s.cluster===lot.cluster||(s.both_clusters&&['cluster_1','cluster_2'].includes(lot.cluster))"));
const suppliers=[{id:1,cluster:'cluster_1',both_clusters:true},{id:2,cluster:'cluster_1'},{id:3,cluster:'cluster_2'}];
assert.deepEqual(filter(suppliers,{cluster:'cluster_2'}).map(s=>s.id),[1,3]);
assert.deepEqual(filter(suppliers,{cluster:'cluster_1'}).map(s=>s.id),[1,2]);
console.log('ui_both_clusters ok');
