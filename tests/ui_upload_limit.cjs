const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const html=fs.readFileSync('procurement/static/index.html','utf8');
const api=html.slice(html.indexOf('async function api('),html.indexOf('function demoApi('));
function context(status=200){
  const calls=[];
  const ctx=vm.createContext({state:{demo:false},FormData,Blob,
    document:{querySelector:()=>null},fetch:async (...args)=>{
      calls.push(args);return {ok:status<400,status,json:async()=>{if(status===413)throw Error('nginx HTML');return {ok:true}}};
    }});
  vm.runInContext(api,ctx);return {ctx,calls};
}
test('100 MiB exact UI accepted, max+1 blocked before HTTP',async()=>{
  const {ctx,calls}=context();
  const small=new FormData();small.append('file',new Blob(['PDF']),'project.pdf');
  await ctx.api('/api/documents',{method:'POST',body:small});assert.equal(calls.length,1);
  class Huge extends Blob {get size(){return 100*1024*1024+1}}
  // FormData subclasses expose metadata without allocating a 100 MiB UI fixture.
  class Input extends FormData {values(){return [new Huge()].values()}}
  await assert.rejects(ctx.api('/api/documents',{method:'POST',body:new Input()}),/Файл больше 100 МБ/);
  assert.equal(calls.length,1);
});
test('nginx HTML 413 becomes understandable message',async()=>{
  const {ctx}=context(413);
  await assert.rejects(ctx.api('/api/documents',{method:'POST',body:new FormData()}),/Файл больше 100 МБ/);
});
test('two individually valid 60 MiB files are refused with aggregate explanation before HTTP',async()=>{
  const {ctx,calls}=context();
  class Medium extends Blob {get size(){return 60*1024*1024}}
  class Input extends FormData {values(){return [new Medium(),new Medium()].values()}}
  await assert.rejects(ctx.api('/api/imports/batch',{method:'POST',body:new Input()}),/Пакет файлов больше 100 МБ/);
  assert.equal(calls.length,0);
  assert.ok(html.includes('100 МБ суммарно на пакет'));
});
