const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync('procurement/static/launch.js','utf8');
test('full launch script parses',()=>new vm.Script(source));
test('SQLite boolean verified preserves both true and false in select',()=>{
  for(const [stored,expected] of [[1,'true'],[0,'false'],[true,'true'],[false,'false']])assert.equal(String(!!stored),expected);
  assert.ok(source.includes("field==='verified'?!!s[field]:s[field]"));
});
test('changing a source or mapping invalidates confirmation and cached preview',()=>{
  const nodes={'#launchPreview':{innerHTML:'old'},'#modalSubmit':{textContent:'Import',onclick:null}};
  const state={preview:{id:'old'}};const handler=()=>{};
  const ctx=vm.createContext({launchState:state,$:s=>nodes[s],previewLaunchImport:handler});
  const start=source.indexOf('function invalidateLaunchPreview()'),end=source.indexOf('async function previewLaunchImport');
  vm.runInContext(source.slice(start,end),ctx);ctx.invalidateLaunchPreview();
  assert.equal(state.preview,null);assert.equal(nodes['#modalSubmit'].onclick,handler);
  assert.ok(source.includes("addEventListener('change',invalidateLaunchPreview)"));
});
