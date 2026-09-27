const {test}=require('node:test');const assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm');
const html=fs.readFileSync('procurement/static/index.html','utf8');
const script=fs.readFileSync('procurement/static/launch.js','utf8');
const source=html.slice(html.indexOf('async function api('),html.indexOf('function demoApi('));
function api(fetch){const ctx=vm.createContext({state:{demo:false},FormData,Blob,URL,location:{href:'https://example.test:9443/'},document:{querySelector:()=>null},fetch});vm.runInContext(source,ctx);return ctx.api}
test('transport failure is honest Russian error, write is never retried',async()=>{
  let calls=0;const request=api(async()=>{calls++;throw new TypeError('Failed to fetch')});
  await assert.rejects(request('/api/suppliers',{method:'POST',body:'{}'}),/Результат операции не подтверждён/);assert.equal(calls,1);
});
test('API validation and login redirect cannot become success',async()=>{
  await assert.rejects(api(async()=>({ok:false,status:422,json:async()=>({detail:'Некорректный телефон'})}))('/api/launch/suppliers/3'),/Некорректный телефон/);
  await assert.rejects(api(async()=>({ok:false,status:422,json:async()=>({detail:[{loc:['body','email'],msg:'invalid email'}]})}))('/api/suppliers'),/Проверьте поля: почта/);
  await assert.rejects(api(async()=>({ok:true,status:200,redirected:true,url:'https://example.test:9443/login'}))('/api/suppliers'),/Сессия завершилась/);
});
test('OCR exposes all lines and requires acknowledgement; no automatic guessed quantities',()=>{
  assert.ok(script.includes('Все строки OCR'));assert.ok(script.includes('Добавить нераспознанную позицию'));
  assert.ok(script.includes("!$('#ocrLinesReviewed')?.checked||!$('#flowHumanApproval')?.checked"));
  assert.ok(script.includes('Проверьте поля')===false); // API helper remains shared, not masked per action.
  assert.ok(script.includes('captureOcrRows()'));assert.ok(script.includes('reviewed_line_ids:s.lines.map'));
  assert.ok(script.includes('<option value="" ${!s.cluster'));
});
