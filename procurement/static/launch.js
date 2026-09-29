/* Only the supplier/import/sheet/attachment launch scope. */
'use strict';
const launchState={config:{smtp_ready:false},preview:null,edit:null};
const baseApi=api;
api=async function(path,options={}){
  const csrf=document.querySelector('meta[name="procurement-launch-csrf"]')?.content;
  if(csrf&&!['GET','HEAD','OPTIONS'].includes((options.method||'GET').toUpperCase()))
    options={...options,headers:{...(options.headers||{}),'X-Launch-CSRF-Token':csrf}};
  return baseApi(path,options);
};
api('/api/launch/config').then(c=>{launchState.config=c;renderRfq()}).catch(()=>{});
function launchModal(title,body,label,handler){
  $('#modalTitle').textContent=title;$('#modalBody').innerHTML=body;
  const submit=$('#modalSubmit');submit.textContent=label;submit.disabled=false;submit.onclick=handler;
  if(!$('#modal').open)$('#modal').showModal();
}
const launchJson=(path,method,data)=>api(path,{method,body:JSON.stringify(data)});
function attachmentChoices(projectId,selected=[]){
  const docs=state.documents.filter(d=>Number(d.project_id)===Number(projectId));
  return docs.length?docs.map(d=>`<label style="display:block"><input type="checkbox" name="launchAttachment" value="${d.id}" ${selected.includes(d.id)?'checked':''}> ${esc(d.filename)} (${(d.size_bytes/1024).toFixed(1)} КБ)</label>`).join(''):'<p>У проекта пока нет файлов. Загрузите их в разделе «Документы» с привязкой к проекту.</p>';
}
function chosenAttachments(root=$('#modalBody')){return [...root.querySelectorAll('input[name="launchAttachment"]:checked')].map(x=>Number(x.value))}
const baseSuppliers=renderSuppliers;
renderSuppliers=function(){
  baseSuppliers();
  document.querySelectorAll('#supplierTable tbody tr').forEach((row,i)=>{
    const s=state.suppliers[i];if(!s)return;
    const cell=document.createElement('td');cell.innerHTML=`<button class="btn secondary small" onclick="editLaunchSupplier(${s.id})">Редактировать</button> <button class="btn secondary small" onclick="changeLaunchSupplier(${s.id},false)">Удалить</button>`;row.append(cell);
  });
  $('#suppliers').insertAdjacentHTML('beforeend','<section class="panel"><button class="btn secondary" onclick="showDeletedSuppliers()">Удалённые поставщики / восстановление</button> <button class="btn secondary" onclick="showSupplierImports()">Импорты и откат</button><div id="launchSupplierHistory"></div></section>');
};
async function editLaunchSupplier(id){
  try{
    const s=await api(`/api/launch/suppliers/${id}`);launchState.edit=s;baseShowModal('supplier');
    $('#modalTitle').textContent='Редактирование поставщика';
    for(const [field,input] of Object.entries({name:'sName',tax_id:'sTax',region:'sRegion',email:'sEmail',phone:'sPhone',telegram:'sTelegram',max_contact:'sMax',rating:'sRating',verified:'sVerified'}))$('#'+input).value=String(field==='verified'?!!s[field]:s[field]);
    $('#sCategories').value=s.categories.join(', ');
    $('#modalBody').insertAdjacentHTML('beforeend',`<label>Кластер<select id="sCluster"><option value="" ${!s.cluster?'selected':''}>По региону / не указан</option><option value="cluster_1" ${s.cluster==='cluster_1'?'selected':''}>Кластер 1</option><option value="cluster_2" ${s.cluster==='cluster_2'?'selected':''}>Кластер 2</option></select></label>`);
    $('#modalSubmit').onclick=()=>modalAction(()=>launchJson(`/api/launch/suppliers/${id}`,'PUT',{
      revision:s.revision,name:$('#sName').value,tax_id:$('#sTax').value,region:$('#sRegion').value,
      email:$('#sEmail').value,phone:$('#sPhone').value,telegram:$('#sTelegram').value,max_contact:$('#sMax').value,
      cluster:$('#sCluster').value,categories:$('#sCategories').value.split(',').map(x=>x.trim()).filter(Boolean),
      rating:Number($('#sRating').value),verified:$('#sVerified').value==='true'
    }),'Поставщик изменён');
  }catch(e){toast(e.message,true)}
}
async function changeLaunchSupplier(id,restore){
  try{
    const s=await api(`/api/launch/suppliers/${id}`);
    if(!confirm(`${restore?'Восстановить':'Удалить из активной базы'} поставщика «${s.name}»? История и документы сохранятся.`))return;
    await launchJson(`/api/launch/suppliers/${id}${restore?'/restore':''}`,restore?'POST':'DELETE',{confirmed:true,revision:s.revision});
    await loadAll();showView('suppliers');toast(restore?'Поставщик восстановлен':'Поставщик удалён из активной базы');
  }catch(e){toast(e.message,true)}
}
async function showDeletedSuppliers(){
  try{const rows=await api('/api/launch/suppliers');$('#launchSupplierHistory').innerHTML=rows.length?rows.map(s=>`<p>${esc(s.name)} · ${esc(s.tax_id)} <button class="btn secondary small" onclick="changeLaunchSupplier(${s.id},true)">Восстановить</button></p>`).join(''):'<p>Удалённых поставщиков нет.</p>'}catch(e){toast(e.message,true)}
}
async function showSupplierImports(){
  try{const rows=await api('/api/launch/imports');$('#launchSupplierHistory').innerHTML=rows.map(r=>`<p>${esc(r.created_at)} · ${esc(r.status)} · добавлено ${r.report.added||0}, обновлено ${r.report.updated||0}, пропущено ${r.report.skipped||0}, ошибок ${r.report.error||0} ${r.status==='applied'?`<button class="btn secondary small" onclick="rollbackSupplierImport('${r.id}')">Откатить</button>`:''}</p>`).join('')||'<p>Импортов нет.</p>'}catch(e){toast(e.message,true)}
}
async function rollbackSupplierImport(id){
  if(!confirm('Откатить этот импорт? Изменённые после импорта карточки не будут перезаписаны.'))return;
  try{const r=await launchJson(`/api/launch/supplier-import/${id}/rollback`,'POST',{confirmed:true});await loadAll();showView('suppliers');toast(`Импорт отменён: ${r.changed} карточек`)}catch(e){toast(e.message,true)}
}
const supplierMapFields={name:'Название',tax_id:'ИНН',region:'Регион',email:'Почта',phone:'Телефон',telegram:'Telegram',max_contact:'MAX',categories:'Категории',rating:'Рейтинг',verified:'Проверен',cluster:'Кластер'};
const sheetMapFields={name:'Позиция',quantity:'Количество',unit:'Единица',specification:'Характеристики',delivery_date:'Срок поставки'};
function mappingMarkup(preview,fields){return `<div class="form-grid">${Object.entries(fields).map(([k,label])=>`<label>${label}<select data-map="${k}"><option value="">Не использовать</option>${preview.headers.map((h,i)=>`<option value="${i}" ${preview.mapping[k]===i?'selected':''}>${i+1}. ${esc(h||'Без заголовка')}</option>`).join('')}</select></label>`).join('')}</div>`}
function openLaunchImport(kind){
  launchState.preview=null;
  const sheet=kind==='sheet';launchState.kind=kind;
  launchModal(sheet?'Заявка из Excel-листа':'Импорт поставщиков Воронежской области',`
    ${sheet?`<label>Проект<select id="launchProject"><option value="">Выберите</option>${state.projects.map(p=>`<option value="${p.id}">${esc(p.name)}</option>`).join('')}</select></label>`:'<label>Регион по умолчанию<input id="launchRegion" value="Воронежская область"></label>'}
    <label>XLSX / CSV<input id="launchFile" type="file" accept=".xlsx,.csv"></label>
    <div class="form-grid"><label>Лист (пусто — первый)<input id="launchSheet"></label><label>Строка заголовков<input id="launchHeader" type="number" min="1" max="100" value="1"></label></div>
    <p>До 100 МБ. Сначала сопоставление колонок и проверка данных. Ничего не создаётся без подтверждения.</p>
    <div id="launchMapping"></div><div id="launchPreview"></div>`,'Предпросмотр',previewLaunchImport);
  for(const selector of ['#launchFile','#launchSheet','#launchHeader',sheet?'#launchProject':'#launchRegion'])$(selector).addEventListener('change',invalidateLaunchPreview);
}
function invalidateLaunchPreview(){launchState.preview=null;$('#launchPreview').innerHTML='<p>Источник или сопоставление изменены. Повторите предпросмотр.</p>';$('#modalSubmit').textContent='Предпросмотр';$('#modalSubmit').onclick=previewLaunchImport}
async function previewLaunchImport(){
  const file=$('#launchFile').files[0];if(!file)return toast('Выберите файл',true);
  const fd=new FormData();fd.append('file',file);fd.append('sheet',$('#launchSheet').value);fd.append('header_row',$('#launchHeader').value);
  const map={};document.querySelectorAll('[data-map]').forEach(s=>{if(s.value!=='')map[s.dataset.map]=Number(s.value)});
  if(Object.keys(map).length)fd.append('mapping',JSON.stringify(map));
  const sheet=launchState.kind==='sheet';
  if(sheet){if(!$('#launchProject').value)return toast('Выберите проект',true);fd.append('project_id',$('#launchProject').value)}
  else fd.append('region',$('#launchRegion').value);
  $('#modalSubmit').disabled=true;
  try{
    const p=await api(`/api/launch/${sheet?'lot-sheet':'supplier-import'}/preview`,{method:'POST',body:fd});launchState.preview=p;
    $('#launchMapping').innerHTML=mappingMarkup(p,sheet?sheetMapFields:supplierMapFields)+`<button class="btn secondary" type="button" onclick="previewLaunchImport()">Повторить с выбранными колонками</button>`;
    document.querySelectorAll('[data-map]').forEach(x=>x.addEventListener('change',invalidateLaunchPreview));
    if(p.needs_mapping){$('#launchPreview').innerHTML='<p>Сопоставьте обязательные колонки и повторите предпросмотр.</p>';return}
    if(sheet){renderSheetReview(p);return}
    const report=p.report;
    $('#launchPreview').innerHTML=`<p>Добавлено: ${report.added}; обновлено: ${report.updated}; пропущено: ${report.skipped}; ошибки: ${report.error}.</p><div class="table-wrap"><table><thead><tr><th>Строка</th><th>Действие</th><th>Поставщик / ИНН</th><th>Ошибка / примечание</th></tr></thead><tbody>${p.rows.slice(0,100).map(r=>`<tr><td>${r.row}</td><td>${esc(r.action)}</td><td>${esc(r.data?.name||'')} ${esc(r.data?.tax_id||'')}</td><td>${esc(r.reason||'')}</td></tr>`).join('')}</tbody></table></div>${p.rows.length>100?'<p>Показаны первые 100 строк; отчёт учитывает весь файл.</p>':''}<label><input id="launchConfirmed" type="checkbox"> Сопоставление и отчёт проверены; импортировать корректные строки</label>`;
    $('#modalSubmit').textContent='Импортировать';$('#modalSubmit').onclick=commitLaunchImport;
  }catch(e){toast(e.message,true)}finally{$('#modalSubmit').disabled=false}
}
async function commitLaunchImport(){
  if(!$('#launchConfirmed')?.checked)return toast('Подтвердите предварительный результат',true);
  try{const r=await launchJson(`/api/launch/supplier-import/${launchState.preview.preview_id}/apply`,'POST',{confirmed:true});$('#modal').close();await loadAll();showView('suppliers');toast(`Импорт: добавлено ${r.added}, обновлено ${r.updated}, пропущено ${r.skipped}, ошибок ${r.error}`)}catch(e){toast(e.message,true)}
}
function renderSheetReview(p){
  const project=state.projects.find(x=>Number(x.id)===Number($('#launchProject').value));
  $('#launchPreview').innerHTML=`<p>Распознано ${p.rows.length} позиций. Исправьте отмеченные строки; ошибки: ${p.errors.length}.</p>
    <div class="form-grid"><label>Название заявки<input id="sheetTitle" value="Закупочная заявка"></label><label>Регион<input id="sheetRegion" value="${esc(project.region)}"></label><label>Адрес<input id="sheetAddress" value="${esc(project.delivery_address)}"></label><label>Ответ до<input id="sheetResponse" type="date" value="${futureDate(7)}"></label><label>Общая дата поставки<input id="sheetDelivery" type="date"></label><label>Валюта<select id="sheetCurrency"><option value="">Выберите</option><option>RUB</option><option>KZT</option><option>USD</option><option>EUR</option></select></label></div>
    <div class="table-wrap"><table><thead><tr><th>Строка</th><th>Позиция</th><th>Количество</th><th>Ед.</th><th>Характеристики</th><th>Срок</th><th></th></tr></thead><tbody id="sheetRows">${p.rows.map(r=>`<tr data-sheet-row="${r.row}"><td>${r.row}${r.error?`<small>${esc(r.error)}</small>`:''}</td><td><input data-field="name" value="${esc(r.name||'')}"></td><td><input data-field="quantity" value="${esc(r.quantity||'')}"></td><td><input data-field="unit" value="${esc(r.unit||'')}"></td><td><textarea data-field="specification">${esc(r.specification||'')}</textarea></td><td><input data-field="delivery_date" type="date" value="${esc(/^\d{4}-\d{2}-\d{2}$/.test(r.delivery_date||'')?r.delivery_date:'')}"></td><td><button type="button" onclick="this.closest('tr').remove()">Убрать</button></td></tr>`).join('')}</tbody></table></div><h4>Файлы проекта для КП</h4>${attachmentChoices(project.id,[p.source_document_id])}<p>Исходный Excel-лист будет прикреплён автоматически.</p><label><input id="sheetConfirmed" type="checkbox"> Все позиции, количества, характеристики и сроки проверены</label>`;
  $('#modalSubmit').textContent='Создать заявку';$('#modalSubmit').onclick=createReviewedSheetLot;
}
async function createReviewedSheetLot(){
  if(!$('#sheetConfirmed').checked)return toast('Подтвердите исправленные позиции',true);
  const items=[...document.querySelectorAll('[data-sheet-row]')].map(row=>{
    const value={};row.querySelectorAll('[data-field]').forEach(x=>value[x.dataset.field]=x.value|| (x.dataset.field==='delivery_date'?null:''));return value;
  });
  const data={confirmed:true,lot:{project_id:Number($('#launchProject').value),title:$('#sheetTitle').value,
    region:$('#sheetRegion').value,delivery_address:$('#sheetAddress').value,response_deadline:$('#sheetResponse').value,
    desired_delivery_date:$('#sheetDelivery').value||null,currency:$('#sheetCurrency').value,
    attachment_document_ids:chosenAttachments(),items}};
  await modalAction(()=>launchJson(`/api/launch/lot-sheet/${launchState.preview.preview_id}/create`,'POST',data),'Заявка создана из проверенного листа');
}
const baseShowModal=showModalForm;
showModalForm=function(type){
  if(type==='supplier-import')return openLaunchImport('supplier');
  if(type==='lot-sheet')return openLaunchImport('sheet');
  baseShowModal(type);
  if(type==='lot'){
    $('#modalBody').insertAdjacentHTML('beforeend','<h4>Файлы проекта для запроса КП</h4><div id="manualAttachments"></div>');
    $('#lProject').addEventListener('change',()=>{$('#manualAttachments').innerHTML=attachmentChoices($('#lProject').value)});
    $('#modalSubmit').onclick=()=>modalAction(()=>launchJson('/api/lots','POST',{
      project_id:Number($('#lProject').value),title:$('#lTitle').value,region:$('#lRegion').value,
      delivery_address:$('#lAddress').value,response_deadline:$('#lDeadline').value,desired_delivery_date:$('#lDelivery').value||null,
      currency:explicitLotCurrency(),attachment_document_ids:chosenAttachments(),
      items:[...document.querySelectorAll('.item-row')].map(r=>({name:r.querySelector('.iName').value,quantity:r.querySelector('.iQty').value,unit:r.querySelector('.iUnit').value,specification:r.querySelector('.iSpec').value}))
    }),'Заявка создана');
  }
};
const baseLots=renderLots;
renderLots=function(){baseLots();$('#lots').insertAdjacentHTML('afterbegin','<button class="btn" style="margin-bottom:16px" onclick="openLaunchImport(\'sheet\')">Создать заявку из Excel / CSV</button>')};
const baseMessagePreview=messagePreview;
messagePreview=function(m){return baseMessagePreview(m)+`<h4>Вложения (${m.attachments?.length||0})</h4>${(m.attachments||[]).map(a=>`<p><a href="/api/launch/documents/${a.document_id}/download">${esc(a.filename)}</a> · ${(a.size_bytes/1024).toFixed(1)} КБ</p>`).join('')}`};
const baseOutbox=outboxMarkup;
const mailStatus={draft:'Черновик',approved:'Черновик подтверждён',queued:'В очереди',sending:'Отправляется',sent:'Отправлен: принят SMTP',failed:'Ошибка отправки',unknown:'Исход не подтверждён; повтор заблокирован'};
function mailJournal(m){const d=m.delivery;if(!d)return '';return `<details><summary>Журнал отправки #${m.id}: ${esc(mailStatus[d.status]||d.status)}</summary><p>Отправитель: ${esc(d.sender||'Не сохранён в старом журнале')}<br>Получатели: ${esc((d.recipients||[m.recipient]).join(', '))}<br>Message-ID: ${esc(d.rfc_message_id||'Не сохранён в старом журнале')}<br>Принят SMTP: ${esc(d.accepted_at||d.updated_at||'нет')}<br>Ответ SMTP: ${esc(d.smtp_code||'не записан')} ${esc(d.smtp_reply||'')}<br>Доставка адресату: не подтверждена</p>${d.error?`<p role="alert">${esc(d.error)}</p>`:''}${d.warning?`<p role="alert">${esc(d.warning)}</p>`:''}<p>Вложения: ${(d.attachments||m.attachments||[]).map(a=>`${esc(a.filename)} · SHA256 ${esc(a.sha256)}`).join('<br>')||'нет'}</p><p>Копия в «Отправленных»: ${d.sent_copy_status==='saved'?'сохранена':'не подтверждена'}</p>${d.copy_retry_allowed?`<button class="btn secondary small" onclick="retrySentCopy(${m.id})">Проверить / сохранить копию (без повторной отправки)</button>`:''}${(d.events||[]).map(e=>`<p>${esc(e.created_at)} · ${esc(e.event)} · ${esc(JSON.stringify(e.details))}</p>`).join('')}</details>`}
function confirmedMailNotice(r){if(r.status!=='sent'||r.accepted_by_smtp!==true)throw new Error('Почтовый сервер не подтвердил отправку');return r.warning||'SMTP принял письмо; копия сохранена в «Отправленных». Доставка адресату пока не подтверждена.'}
async function retrySentCopy(id){try{const r=await launchJson(`/api/launch/outbox/${id}/sent-copy`,'POST',{confirmed:true});await loadAll();toast(confirmedMailNotice(r),!!r.warning)}catch(e){toast(e.message,true)}}
outboxMarkup=function(){return baseOutbox()+`<section class="panel"><h3>Фактическая отправка email-запросов</h3><p>${launchState.config.smtp_ready?'Статус «Отправлен» означает приём SMTP, но не доставку адресату.':'SMTP не настроен. Черновик не считается отправкой.'}</p>${state.outbox.filter(m=>m.channel==='email').map(m=>`<article><p>#${m.id} · ${esc(m.supplier_name)} · ${esc(mailStatus[m.delivery?.status||m.status]||m.status)} · ${m.attachments?.length||0} вложений ${m.status==='approved'||m.delivery?.retry_allowed?`<button class="btn small" onclick="sendLaunchMessage(${m.id})" ${launchState.config.smtp_ready?'':'disabled'}>${m.delivery?.retry_allowed?'Безопасный повтор':'Отправить с вложениями'}</button>`:''}</p>${mailJournal(m)}</article>`).join('')}</section>`};
async function sendLaunchMessage(id){
  const m=state.outbox.find(x=>x.id===id);if(!m)return;
  launchModal('Отправка запроса КП',messagePreview(m)+'<label><input id="sendConfirmed" type="checkbox"> Подтверждаю реальную отправку этому адресату со всеми указанными вложениями</label>','Отправить',async()=>{
    if(!$('#sendConfirmed').checked)return toast('Подтвердите отправку',true);
    const button=$('#modalSubmit');button.disabled=true;
    try{const r=await launchJson(`/api/launch/outbox/${id}/send`,'POST',{confirmed:true});const notice=confirmedMailNotice(r);$('#modal').close();await loadAll();toast(notice,!!r.warning)}catch(e){await loadAll();toast(e.message,true)}finally{button.disabled=false}
  });
}
const baseTender=renderTender;
renderTender=function(){baseTender();const lot=state.tenderLot;if(!lot)return;$('#tender').insertAdjacentHTML('beforeend',`<section class="panel"><h3>Вложения заявки</h3><div id="tenderAttachmentChoices">${attachmentChoices(lot.project_id,(lot.attachments||[]).map(a=>a.document_id))}</div><button class="btn secondary" onclick="saveLaunchAttachments(${lot.id})">Сохранить вложения до подготовки КП</button></section>`)};
async function saveLaunchAttachments(id){try{await launchJson(`/api/launch/lots/${id}/attachments`,'PUT',{document_ids:chosenAttachments($('#tenderAttachmentChoices'))});await openLot(id);toast('Вложения заявки сохранены')}catch(e){toast(e.message,true)}}

const baseDocumentFlow=renderDocumentFlow;
renderDocumentFlow=function(){
  let markup=baseDocumentFlow();
  markup=markup.replace('Для пилота ограждений используется профиль «Булат». Выберите объект, загрузите PDF и укажите лист со спецификацией.',
    'Выберите объект и лист спецификации. Сканированный PDF распознаётся на русском языке; позиции и количества нужно проверить перед созданием заявки.')
    .replace('id="flowPage" type="number" min="1" max="10000" value="13"','id="flowPage" type="number" min="1" max="10000" value="1"');
  const s=state.documentFlow.suggestion;
  if(documentFlowStage()!==2||s?.review_kind!=='pdf_ocr')return markup;
  const start=markup.indexOf('<div class="trace-list">'),end=markup.indexOf('</div><div class="doc-flow-box" style="margin-top:14px">',start);
  if(start<0||end<0)return markup;
  const rows=s.items.map((r,i)=>`<tr data-ocr-row="${i}"><td><input aria-label="Наименование ${i+1}" data-ocr-field="name" value="${esc(r.name||'')}" oninput="captureOcrRows()"></td><td><input aria-label="Количество ${i+1}" data-ocr-field="quantity" value="${esc(r.quantity||'')}" oninput="captureOcrRows()"></td><td><input aria-label="Единица ${i+1}" data-ocr-field="unit" value="${esc(r.unit||'')}" oninput="captureOcrRows()"></td><td><textarea aria-label="Характеристики ${i+1}" data-ocr-field="specification" oninput="captureOcrRows()">${esc(r.specification||'')}</textarea></td><td><button type="button" class="btn secondary small" onclick="removeOcrRow(${i})">Убрать</button></td></tr>`).join('');
  const review=`<div class="ocr-review"><p><b>OCR требует ручной проверки.</b> Количество и единица не подставляются из размеров чертежа или массы. Нераспознанные строки показаны ниже; добавьте пропущенные позиции вручную.</p>
    <label>Название заявки<input id="ocrLotTitle" value="${esc(s.lot_title)}" oninput="state.documentFlow.suggestion.lot_title=this.value"></label>
    <div class="table-wrap"><table><thead><tr><th>Наименование</th><th>Количество</th><th>Единица</th><th>Характеристики</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>
    <button type="button" class="btn secondary" onclick="addOcrRow()">Добавить нераспознанную позицию</button>
    <h4>Все строки OCR (${s.lines.length}), включая нераспознанные</h4><p>Сверьте с оригиналом: <a target="_blank" rel="noopener" href="/api/launch/documents/${s.source_document_id}/download">Открыть исходный PDF</a>. Исходный файл будет приложен к КП без изменения.</p>
    <textarea readonly aria-label="Все строки OCR" rows="12" style="width:100%">${esc(s.lines.map(l=>`${l.line}. ${l.text} [${Math.round(l.confidence*100)}%]`).join('\n'))}</textarea>
    <label><input id="ocrLinesReviewed" type="checkbox"> Я проверил все строки OCR по оригиналу, добавил пропущенные позиции и явно исключил остальные строки как не относящиеся к заявке.</label></div>`;
  return markup.slice(0,start)+review+markup.slice(end);
};
function captureOcrRows(){
  const s=state.documentFlow.suggestion;if(s?.review_kind!=='pdf_ocr')return;
  s.items=[...document.querySelectorAll('[data-ocr-row]')].map(row=>{
    const data={};row.querySelectorAll('[data-ocr-field]').forEach(x=>data[x.dataset.ocrField]=x.value);return data;
  });
  if($('#ocrLinesReviewed'))$('#ocrLinesReviewed').checked=false;
  if($('#flowHumanApproval'))$('#flowHumanApproval').checked=false;
}
function addOcrRow(){captureOcrRows();state.documentFlow.suggestion.items.push({name:'',quantity:'',unit:'',specification:''});captureDocumentRequirements();renderDocuments()}
function removeOcrRow(i){captureOcrRows();state.documentFlow.suggestion.items.splice(i,1);captureDocumentRequirements();renderDocuments()}
const baseApproveDocumentFlow=approveDocumentFlow;
approveDocumentFlow=async function(){
  const f=state.documentFlow,s=f.suggestion;
  if(s?.review_kind!=='pdf_ocr')return baseApproveDocumentFlow();
  if(!$('#ocrLinesReviewed')?.checked||!$('#flowHumanApproval')?.checked)return toast('Подтвердите проверку всех строк OCR и позиций по оригиналу',true);
  const req=captureDocumentRequirements();
  if(!s.items.length||s.items.some(r=>!r.name?.trim()||!/^\d+(?:[.,]\d+)?$/.test(r.quantity||'')||Number((r.quantity||'').replace(',','.'))<=0||!r.unit?.trim()))return toast('Исправьте наименования, положительные количества и единицы всех позиций',true);
  if(!req.response_deadline||!req.delivery_address_confirmation)return toast('Заполните адрес доставки и срок ответа',true);
  const project=state.projects.find(p=>Number(p.id)===Number(s.project_id)),button=$('#flowApproveBtn');button.disabled=true;
  try{
    const lot=await launchJson(`/api/launch/pdf-review/${s.id}/create`,'POST',{
      confirmed:true,reviewed_line_ids:s.lines.map(l=>l.line),lot:{project_id:s.project_id,title:s.lot_title,
        region:project.region,delivery_address:req.delivery_address_confirmation,response_deadline:req.response_deadline,
        desired_delivery_date:req.desired_delivery_date||null,currency:'RUB',
        items:s.items.map(r=>({...r,quantity:r.quantity.replace(',','.')}))}});
    f.lot=lot;
    try{f.matches=await api(`/api/lots/${lot.id}/supplier-matches`)}catch{f.matches=[]}
    const refreshed=await loadAll();showView('documents');
    toast(refreshed?'Заявка создана после проверки OCR. Исходный PDF приложен.':'Заявка сохранена; обновление списка не удалось. Обновите страницу, не создавайте повторно.',!refreshed);
  }catch(e){toast(e.message,true)}finally{button.disabled=false}
};
render();
