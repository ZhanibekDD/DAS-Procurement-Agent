/* Synthetic timing regressions against shipped UI. No server or browser acceptance. */
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const html=fs.readFileSync(path.join(__dirname,'../procurement/static/index.html'),'utf8');
const script=html.match(/<script>([\s\S]*?)<\/script>/)[1];
const section=(start,end)=>script.slice(script.indexOf(start),script.indexOf(end));
const deferred=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no});return {promise,resolve,reject}};
const tick=()=>new Promise(resolve=>setImmediate(resolve));

function harness(api,archive=async()=>{}){
  const buttons=['lot','document','quote','price-history','project'].map(open=>({dataset:{open}}));
  const nodes={};
  for(const id of ['modal','modalBody','modalTitle','modalSubmit','pName','pRegion','pAddress','pDescription'])
    nodes['#'+id]={value:'Synthetic',innerHTML:'',classList:{add(){},remove(){}}};
  let opens=0,closes=0,renders=0;
  nodes['#modal'].showModal=()=>{opens++};nodes['#modal'].close=()=>{closes++};
  const messages=[];
  const state={projects:[],suppliers:[],view:'projects',demo:false};
  const ctx=vm.createContext({state,$:selector=>nodes[selector],document:{querySelectorAll:()=>buttons},
    api,loadArchive:archive,setConnection(){},toast:(text,error)=>messages.push({text,error}),
    render:()=>{renders++},showView(){},setTimeout(){},createLot(){}});
  vm.runInContext(section('const esc=','const money=')+'\n'+section('const refreshUI=','function setConnection')+
    '\n'+section('function bindOpeners()','function addItemRow()')+'\n'+section('async function modalAction(','const commitSupplierImport='),ctx);
  return {ctx,state,nodes,buttons,messages,get opens(){return opens},get closes(){return closes},get renders(){return renders}};
}

test('fast click cannot snapshot empty projects while GET is pending; ready form uses new project',async()=>{
  const gate=deferred(),h=harness(url=>url==='/api/projects'?gate.promise:Promise.resolve([]));
  const refresh=h.ctx.loadAll();
  assert.ok(h.buttons.every(button=>button.disabled));
  h.ctx.openModal('lot');assert.equal(h.opens,0);
  gate.resolve([{id:41,name:'Synthetic created project'}]);await refresh;
  assert.equal(h.buttons[0].disabled,false);
  h.ctx.openModal('lot');assert.equal(h.opens,1);
  assert.ok(h.nodes['#modalBody'].innerHTML.includes('value="41"'));
});

test('dependent modal is blocked on first load and after failed refresh until GET retry succeeds',async()=>{
  let fail=true,requests=0;
  const h=harness(async url=>{requests++;if(fail&&url==='/api/projects')throw Error('Synthetic refresh failure');return []});
  h.ctx.bindOpeners();h.ctx.openModal('lot');assert.equal(h.opens,0);
  assert.equal(await h.ctx.loadAll(),false);
  h.ctx.openModal('lot');assert.equal(h.opens,0);assert.equal(h.buttons[0].disabled,true);
  fail=false;assert.equal(await h.ctx.loadAll(),true);h.ctx.openModal('lot');assert.equal(h.opens,1);
  assert.equal(requests,20);
});

test('newly rendered openers remain disabled until archive/render is complete',async()=>{
  const gate=deferred(),h=harness(async()=>[],()=>gate.promise);
  const refresh=h.ctx.loadAll();await tick();h.ctx.bindOpeners();
  assert.ok(h.buttons.every(button=>button.disabled));assert.equal(h.renders,0);
  gate.resolve();assert.equal(await refresh,true);assert.equal(h.renders,1);assert.equal(h.buttons[0].disabled,false);
});

test('actual project submit cannot repeat during POST or delayed refresh and opens lot only when ready',async()=>{
  const post=deferred(),get=deferred();let posts=0;
  const h=harness((url,options)=>{if(options?.method==='POST'){posts++;return post.promise}return url==='/api/projects'?get.promise:Promise.resolve([])});
  const save=vm.runInContext('createProject()',h.ctx);await tick();
  await vm.runInContext('createProject()',h.ctx);assert.equal(posts,1);assert.equal(h.closes,0);
  post.resolve({id:51});await tick();assert.equal(h.closes,1);h.ctx.openModal('lot');assert.equal(h.opens,0);
  await vm.runInContext('createProject()',h.ctx);assert.equal(posts,1);
  get.resolve([{id:51,name:'Saved once'}]);await save;
  h.ctx.openModal('lot');assert.ok(h.nodes['#modalBody'].innerHTML.includes('value="51"'));
  assert.equal(posts,1);assert.equal(h.nodes['#modalSubmit'].disabled,false);
});

test('saved project plus refresh failure reports saved, never repeats POST, and GET retry recovers',async()=>{
  let fail=true,posts=0;
  const h=harness(async(url,options)=>{if(options?.method==='POST'){posts++;return {id:61}}if(fail&&url==='/api/projects')throw Error('Synthetic offline');return url==='/api/projects'?[{id:61,name:'Saved project'}]:[]});
  h.state.projects=[{id:60,name:'Existing'}];await vm.runInContext('createProject()',h.ctx);
  assert.equal(posts,1);assert.equal(h.closes,1);assert.equal(h.state.projects[0].id,60);
  assert.match(h.messages.at(-1).text,/Проект создан.*Повторять сохранение не нужно/);
  assert.equal(h.messages.at(-1).error,true);assert.equal(h.buttons[0].disabled,true);
  fail=false;await h.ctx.loadAll();assert.equal(posts,1);assert.equal(h.state.projects[0].id,61);
});

test('POST failure keeps form available for explicit retry and never starts refresh',async()=>{
  let gets=0;
  const h=harness(async(url,options)=>{if(options?.method==='POST')throw Error('Synthetic validation error');gets++;return []});
  await vm.runInContext('createProject()',h.ctx);
  assert.equal(h.closes,0);assert.equal(gets,0);assert.equal(h.nodes['#modalSubmit'].disabled,false);
  assert.equal(h.messages.at(-1).text,'Synthetic validation error');
});

test('out-of-order refresh cannot overwrite newly created project or enable buttons too early',async()=>{
  const old=deferred(),latest=deferred();let count=0;
  const h=harness(url=>url==='/api/projects'?(++count===1?old.promise:latest.promise):Promise.resolve([]));
  const first=h.ctx.loadAll(),second=h.ctx.loadAll();latest.resolve([{id:71,name:'Newest'}]);
  assert.equal(await second,true);assert.equal(h.buttons[0].disabled,true);
  old.resolve([]);assert.equal(await first,false);assert.equal(h.state.projects[0].id,71);
  assert.equal(h.buttons[0].disabled,false);
});

test('existing modal cannot submit while another refresh is pending',async()=>{
  const gate=deferred();let posts=0;
  const h=harness((url,options)=>{if(options?.method==='POST'){posts++;return Promise.resolve({id:1})}return url==='/api/projects'?gate.promise:Promise.resolve([])});
  const refresh=h.ctx.loadAll();await vm.runInContext('createProject()',h.ctx);assert.equal(posts,0);
  gate.resolve([]);await refresh;
});
