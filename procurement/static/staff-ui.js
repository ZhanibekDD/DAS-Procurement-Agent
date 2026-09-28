'use strict';
/* Presentation only: the server remains the authority for ACL, lot contents and SMTP status. */
Object.assign(mailStatus, {
  draft: 'Черновик', approved: 'Готов к отправке', queued: 'Готов к отправке',
  sending: 'Отправляется', sent: 'Отправлен', failed: 'Не отправлено',
  unknown: 'Результат отправки не подтверждён'
});
const statusNames = {
  draft:'Черновик', rfq_draft:'Черновик', rfq_sent:'Запрос отправлен',
  queued:'Готов к отправке', approved:'Подтверждён', sending:'Отправляется',
  sent:'Отправлен', failed:'Не отправлено', unknown:'Результат отправки не подтверждён',
  quotes_received:'Получены цены', comparison:'Сравнение', awarded:'Поставщик выбран',
  ordered:'Заказ', cancelled:'Отменён', active:'Активен', rejected:'Отклонён',
  needs_review:'Требует проверки', pending_ai_extraction:'Обрабатывается',
  imported:'Импортирован', confirmed:'Подтверждён', completed:'Завершён',
  applied:'Применён', rolled_back:'Отменён'
};
function humanStatus(value) { return statusNames[value] || 'Статус уточняется'; }
const technicalBadge = badge;
badge = function(value) {
  if (Object.hasOwn(statusNames, value)) return `<span class="badge">${esc(humanStatus(value))}</span>`;
  return technicalBadge(value);
};

function staffMailStatus(message) {
  const delivery = message.delivery;
  if (delivery?.status === 'sent' && !delivery.accepted_at && !delivery.legacy)
    return 'Результат отправки не подтверждён';
  return mailStatus[delivery?.status || message.status] || 'Результат отправки не подтверждён';
}

function staffMailError(value) {
  const text = String(value || '');
  if (/timeout|timed out|время ожидания/i.test(text)) return 'Почтовый сервер не ответил вовремя.';
  if (/recipient|адрес получателя|mailbox/i.test(text)) return 'Почтовый сервер отклонил адрес получателя.';
  if (/connect|network|connection|соединен/i.test(text)) return 'Нет связи с почтовым сервером.';
  return 'Письмо не отправлено. Проверьте данные и попробуйте снова.';
}

const technicalMailJournal = mailJournal;
mailJournal = function(message) {
  if (state.role === 'admin') return technicalMailJournal(message);
  const delivery = message.delivery;
  if (!delivery) return '';
  const files = delivery.attachments || message.attachments || [];
  return `<details><summary>Отправка: ${esc(staffMailStatus(message))}</summary>
    <p>Получатель: ${esc(message.recipient)}<br>Время: ${esc(delivery.accepted_at || delivery.updated_at || 'ещё не отправлено')}<br>
    Копия в «Отправленных»: ${delivery.sent_copy_status === 'saved' ? 'сохранена' : 'не подтверждена'}<br>
    Вложения: ${files.length ? files.map(file => esc(file.filename)).join(', ') : 'нет'}</p>
    ${delivery.error ? `<p role="alert">${esc(staffMailError(delivery.error))}</p>` : ''}
    ${delivery.warning ? `<p role="alert">${esc(delivery.warning)}</p>` : ''}
    ${delivery.copy_retry_allowed ? `<button class="btn secondary small" onclick="retrySentCopy(${Number(message.id)})">Проверить копию без повторной отправки</button>` : ''}
  </details>`;
};

function moreMenu(buttons, title='Ещё') {
  const box = document.createElement('details');
  box.className = 'action-more';
  const summary = document.createElement('summary');
  summary.textContent = title;
  box.append(summary, ...buttons);
  return box;
}

function openActivity(view, id) {
  if (view === 'lots') return openLot(id);
  if (view === 'projects') return openPortfolio(id);
  if (view === 'documents') return viewProcurementDocument(id);
  showView(view);
  if (view === 'suppliers') {
    const index = state.suppliers.findIndex(s => Number(s.id) === Number(id));
    if (index >= 0) {
      const row = document.querySelectorAll('#supplierTable tbody tr')[index];
      row?.scrollIntoView({block:'center'});
      row?.classList.add('activity-target');
    } else {
      const history = document.querySelector('#launchSupplierHistory')?.parentElement;
      if (history) history.hidden = false;
      showDeletedSuppliers();
    }
  }
}

function staffActivity() {
  const activities = state.activity || [];
  return `<section class="panel recent-activity"><h2>Последние действия</h2><div class="activity">${activities.length
    ? activities.map(item => `<div class="activity-item"><i></i><div><b>${esc(item.label)}</b><br>
      <button class="activity-link" type="button" onclick="openActivity('${esc(item.view)}',${Number(item.target_id)})">${esc(item.name)}</button>
      <small>${date(item.created_at)}</small></div></div>`).join('')
    : '<p>Пока нет новых действий.</p>'}</div></section>`;
}

function staffLotCounts(lots) {
  const stages = [
    ['Черновики', ['draft', 'rfq_draft']],
    ['Запрос отправлен', ['rfq_sent']],
    ['Получены цены', ['quotes_received', 'comparison']],
    ['Поставщик выбран', ['awarded', 'ordered']]
  ];
  return stages.map(([label, statuses]) => ({
    label, count: lots.filter(lot => statuses.includes(lot.status)).length
  }));
}

renderOverview = function() {
  $('#overview').innerHTML = `<section class="panel"><div class="panel-title"><h2>Закупки в работе</h2>
    <button class="btn" data-open="lot">Новая закупка</button></div>${lotsTable(state.lots.slice(0,6))}</section>${staffActivity()}`;
  bindOpeners();
};

const fullRenderLots = renderLots;
const quickPurchase = {intake:null, busy:false, recovered:false};
function quickPurchaseMarkup() {
  const projectOptions=state.projects.map(p=>`<option value="${Number(p.id)}">${esc(p.name)}</option>`).join('');
  return `<section class="panel quick-purchase"><div class="panel-title"><div><h2>Новая закупка из файла</h2><p>Загрузите PDF или Excel. Черновик, поставщики, письмо и вложение появятся здесь.</p></div></div>
    <div class="toolbar"><label>Объект<select id="quickProject"><option value="">Выберите объект</option>${projectOptions}</select></label>
    <label>Спецификация PDF / XLSX / CSV<input id="quickFile" type="file" accept=".pdf,.xlsx,.csv" ${quickPurchase.busy?'disabled':''}></label></div>
    <div id="quickProgress" role="status">${quickPurchase.busy?'Распознаём файл и сохраняем черновик…':''}</div>
    <div id="quickReview"></div></section>`;
}
renderLots = function() {
  fullRenderLots();
  const root = $('#lots');
  root.insertAdjacentHTML('afterbegin',quickPurchaseMarkup());
  $('#quickProject').value=quickPurchase.intake?.document?.project_id || (state.projects.length===1?state.projects[0].id:'');
  $('#quickFile').onchange=startQuickPurchase;
  if(quickPurchase.intake?.status==='needs_review')renderQuickReview();
  const summary = root.querySelector('.lot-summary');
  if (summary) {
    const stages = staffLotCounts(state.lots);
    summary.querySelectorAll('.mini-stat').forEach((card, index) => {
      card.querySelector('span').textContent = stages[index].label;
      card.querySelector('b').textContent = stages[index].count;
    });
  }
  root.querySelectorAll(':scope > section.panel').forEach(panel => {
    if (panel.querySelector('h2')?.textContent === 'Закупочный процесс') panel.remove();
  });
  const title = root.querySelector('#lotsTable')?.closest('section.panel')?.querySelector('.panel-title');
  if (title) {
    title.querySelector('h2').textContent = 'Список закупок';
    title.querySelector('p')?.remove();
  }
  const create = root.querySelector('[data-open="lot"]');
  if (create) create.textContent = 'Новая закупка';
  root.querySelectorAll('#lotsTable tbody td:first-child small.muted').forEach(node => node.remove());
  const excel = root.querySelector(':scope > button[onclick*="openLaunchImport"]');
  if (excel) {
    excel.classList.add('secondary');
    excel.textContent = 'Создать из Excel или CSV';
    const title = root.querySelector('#lotsTable')?.closest('section.panel')?.querySelector('.panel-title');
    title?.append(moreMenu([excel]));
  }
  root.insertAdjacentHTML('beforeend', staffActivity());
};

const fullRenderProjects = renderProjects;
renderProjects = function() {
  fullRenderProjects();
  $('#projects').querySelectorAll('.project-number').forEach(node => node.remove());
};

const fullRenderSuppliers = renderSuppliers;
renderSuppliers = function() {
  fullRenderSuppliers();
  const root = $('#suppliers');
  const hero = root.querySelector('.supplier-visual');
  const panelTitle = root.querySelector('.panel-title');
  panelTitle?.querySelector('p')?.remove();
  const add = hero?.querySelector('[data-open="supplier"]');
  const importButton = hero?.querySelector('[data-open="supplier-import"]');
  const history = root.querySelector('#launchSupplierHistory')?.parentElement;
  const historyButtons = [...(history?.querySelectorAll('button') || [])];
  if (add) { add.textContent = 'Добавить поставщика'; panelTitle?.append(add); }
  if (importButton || historyButtons.length) panelTitle?.append(moreMenu([importButton, ...historyButtons].filter(Boolean)));
  hero?.remove();
  if (history) { history.classList.add('supplier-history'); history.hidden = true; }
  if (importButton) importButton.addEventListener('click', () => { if (history) history.hidden = false; });
  historyButtons.forEach(button => button.addEventListener('click', () => { if (history) history.hidden = false; }));
  root.querySelectorAll('#supplierTable tbody tr').forEach(row => {
    const actions = row.lastElementChild;
    if (actions?.querySelector('button[onclick*="editLaunchSupplier"]')) actions.replaceChildren(moreMenu([...actions.querySelectorAll('button')]));
  });
  bindOpeners();
};

async function startQuickPurchase() {
  const projectId=Number($('#quickProject').value),file=$('#quickFile').files[0];
  if(!file)return;
  if(!projectId){$('#quickFile').value='';return toast('Сначала выберите объект',true)}
  if(file.size>100*1024*1024){$('#quickFile').value='';return toast('Файл больше 100 МБ',true)}
  quickPurchase.busy=true;$('#quickFile').disabled=true;
  $('#quickProgress').textContent='Распознаём файл и сохраняем черновик…';
  try{
    const form=new FormData();form.append('project_id',String(projectId));form.append('file',file);
    const result=await api('/api/procurement/quick-intake',{method:'POST',body:form});
    quickPurchase.intake=result;
    await loadAll();
    if(result.status==='draft'){
      toast('Черновик сохранён. Проверьте получателей, письмо и вложение.');
      await openLot(result.lot.id);
      if($('#quickProgress'))$('#quickProgress').textContent='Черновик сохранён. Ничего не отправлено.';
    }else{
      showView('lots');$('#quickProgress').textContent=result.reason;
      $('#quickReview')?.scrollIntoView({block:'nearest'});
    }
  }catch(error){$('#quickProgress').textContent='Черновик не подтверждён: '+error.message;toast(error.message,true)}
  finally{quickPurchase.busy=false;if($('#quickFile'))$('#quickFile').disabled=false}
}

function renderQuickReview() {
  const result=quickPurchase.intake,draft=result?.draft;if(!draft)return;
  const rows=draft.rows||[];
  const source=`/api/launch/documents/${Number(result.document.id)}/download`;
  $('#quickReview').innerHTML=`<h3>Проверьте сомнительные позиции</h3><p>${esc(result.reason)}</p>
    <p><a href="${source}" target="_blank" rel="noopener">Открыть исходный файл</a>. Он приложится к запросу без изменений.</p>
    ${draft.needs_mapping?`<p role="alert">Колонки не распознаны. Укажите их один раз:</p><div class="toolbar">${[
      ['name','Позиция'],['quantity','Количество'],['unit','Единица'],['specification','Характеристики'],['delivery_date','Срок']
    ].map(([key,label])=>`<label>${label}<select data-quick-map="${key}"><option value="">Не использовать</option>${(draft.headers||[]).map((name,index)=>`<option value="${index}" ${draft.mapping?.[key]===index?'selected':''}>${esc(name)}</option>`).join('')}</select></label>`).join('')}</div><button class="btn secondary" type="button" onclick="remapQuickDraft()">Распознать по колонкам</button>`:''}
    <div class="table-wrap"><table><thead><tr><th>Строка</th><th>Позиция</th><th>Количество</th><th>Ед.</th><th>Характеристики</th><th>Срок</th><th></th></tr></thead><tbody id="quickRows">
    ${rows.map((r,i)=>`<tr data-quick-row="${i}" class="${r.error?'needs-review':''}"><td>${esc(r.row)}${r.error?`<small role="alert">${esc(r.error)}</small>`:''}</td>
    <td><input data-quick-field="name" value="${esc(r.name||'')}"></td><td><input data-quick-field="quantity" value="${esc(r.quantity||'')}"></td>
    <td><input data-quick-field="unit" value="${esc(r.unit||'')}"></td><td><input data-quick-field="specification" value="${esc(r.specification||'')}"></td>
    <td><input data-quick-field="delivery_date" type="date" value="${esc(r.delivery_date||'')}"></td>
    <td><button class="btn secondary small" type="button" onclick="this.closest('tr').remove()">Исключить</button></td></tr>`).join('')}</tbody></table></div>
    ${result.kind==='pdf'?`<details open><summary>Все распознанные строки PDF (${draft.lines?.length||0}) — проверьте пропуски</summary><pre>${esc((draft.lines||[]).map(l=>`${l.line}. ${l.text}`).join('\n'))}</pre></details>`:''}
    <button class="btn secondary" type="button" onclick="addQuickRow()">Добавить пропущенную позицию</button>
    <button class="btn" type="button" id="quickReviewDone" onclick="finishQuickReview()" ${draft.needs_mapping?'disabled':''}>${result.kind==='pdf'?'Проверил исходный PDF и позиции — показать запрос':'Проверил позиции — показать запрос'}</button>
    <p>Ни одно письмо не отправлено. После проверки позиций вы увидите получателей и текст запроса.</p>`;
}
async function remapQuickDraft(){
  const intake=quickPurchase.intake,mapping={};
  document.querySelectorAll('[data-quick-map]').forEach(select=>{if(select.value!=='')mapping[select.dataset.quickMap]=Number(select.value)});
  try{
    const updated=await launchJson(`/api/procurement/quick-draft/${intake.draft.preview_id}/remap`,'POST',{mapping});
    quickPurchase.intake=updated;renderLots();toast('Колонки сопоставлены. Проверьте отмеченные строки.');
  }catch(error){toast(error.message,true)}
}
const quickBaseLoadAll=loadAll;
loadAll=async function(){
  const loaded=await quickBaseLoadAll();
  if(loaded&&!state.demo&&!quickPurchase.recovered){
    quickPurchase.recovered=true;
    try{
      const saved=await api('/api/procurement/quick-draft');
      if(saved&&!quickPurchase.intake){quickPurchase.intake=saved;if(state.view==='lots')renderLots()}
    }catch{/* The main dashboard remains usable; no unconfirmed draft is sent. */}
  }
  return loaded;
};
function addQuickRow(){
  const tbody=$('#quickRows'),row=tbody.querySelector('tr')?.cloneNode(true);
  if(!row){tbody.insertAdjacentHTML('beforeend',`<tr class="needs-review"><td>Добавлено вручную</td>
    <td><input data-quick-field="name"></td><td><input data-quick-field="quantity"></td>
    <td><input data-quick-field="unit"></td><td><input data-quick-field="specification"></td>
    <td><input data-quick-field="delivery_date" type="date"></td>
    <td><button class="btn secondary small" type="button" onclick="this.closest('tr').remove()">Исключить</button></td></tr>`);return}
  row.querySelectorAll('input').forEach(input=>input.value='');row.querySelector('td').textContent='Добавлено вручную';
  row.classList.add('needs-review');tbody.append(row);
}
async function finishQuickReview(){
  const result=quickPurchase.intake,draft=result?.draft;
  if(!draft||draft.needs_mapping)return toast('Сначала сопоставьте колонки',true);
  const items=[...document.querySelectorAll('#quickRows tr')].map(row=>{
    const data={};row.querySelectorAll('[data-quick-field]').forEach(input=>data[input.dataset.quickField]=input.value.trim());
    data.delivery_date ||= null;return data;
  });
  if(!items.length||items.some(i=>!i.name||!i.unit||!/^\d+(?:[.,]\d+)?$/.test(i.quantity)||Number(i.quantity.replace(',','.'))<=0))
    return toast('Исправьте наименование, количество и единицу каждой оставленной позиции',true);
  const project=state.projects.find(p=>Number(p.id)===Number(result.document.project_id));
  if(!project)return toast('Объект не найден; обновите страницу',true);
  const payload={confirmed:true,lot:{project_id:project.id,title:result.document.filename.replace(/\.[^.]+$/,'').slice(0,240),
    region:project.region,delivery_address:project.delivery_address,response_deadline:futureDate(7),currency:'RUB',
    attachment_document_ids:[result.document.id],items}};
  if(result.kind==='pdf')payload.reviewed_line_ids=(draft.lines||[]).map(line=>line.line);
  const button=$('#quickReviewDone');button.disabled=true;
  try{
    const endpoint=result.kind==='pdf'?`/api/launch/pdf-review/${draft.preview_id}/create`:`/api/launch/lot-sheet/${draft.preview_id}/create`;
    const lot=await launchJson(endpoint,'POST',payload);
    quickPurchase.intake={status:'draft',lot,document:result.document};
    await loadAll();toast('Черновик сохранён. Проверьте письмо и отправьте запрос.');await openLot(lot.id);
  }catch(error){toast(error.message,true)}finally{if(button.isConnected)button.disabled=false}
}

const fullRenderRfq = renderRfq;
renderRfq = function() {
  fullRenderRfq();
  const root = $('#rfq');
  const lot = purchasing.lot?.id === state.selectedLot ? purchasing.lot : null;
  const header = root.querySelector(':scope > section.panel');
  header?.querySelector('p')?.remove();
  if (lot) header?.insertAdjacentHTML('beforeend', `<small class="purchase-stage">Этап: ${esc(procurementStages[procurementStage(lot)])}</small>`);
  root.querySelectorAll('#procurementLot option').forEach(option => { option.textContent = option.textContent.replace(/^#\d+\s*·\s*/, ''); });
  const title = root.querySelector('h3');
  if (title?.textContent.startsWith('Позиции лота')) title.textContent = 'Позиции закупки';
  const sourceNote = [...root.querySelectorAll('p')].find(p => p.textContent.includes('immutable ID'));
  sourceNote?.remove();
  root.querySelectorAll('.message-card').forEach((card, index) => {
    const message = filteredProcurementMessages(state.selectedLot)[index];
    const line = card.querySelector('p');
    if (message && line) line.textContent = `${message.supplier_name} · ${staffMailStatus(message)}`;
    const actions = [...card.querySelectorAll(':scope > button')];
    if(message?.delivery?.retry_allowed && actions.length){
      const retry=actions[0];retry.textContent='Повторить';retry.className='btn small';
      retry.onclick=()=>sendSavedProcurementMessage(message.id);
      return;
    }
    if (message?.status === 'draft' && actions.length) {
      const button = actions[0];
      button.textContent = state.role === 'admin' ? 'Проверить правило согласования' : 'Отправить запрос';
      if (state.role !== 'admin') button.onclick = () => {
        if (confirm(`Отправить запрос поставщику «${message.supplier_name}»?`)) sendSavedProcurementMessage(message.id);
      };
    }
    if (actions.length) card.append(moreMenu(actions));
  });
  const side = root.querySelector('.layout > section.panel:last-child');
  const secondary = [...(side?.querySelectorAll(':scope > button') || [])];
  if (secondary.length) side.append(moreMenu(secondary));
};

const fullRenderPricebook = renderPricebook;
renderPricebook = function() {
  fullRenderPricebook();
  const root = $('#pricebook');
  root.querySelector('.price-hero')?.remove();
  const catalog = root.querySelector(':scope > section.panel');
  catalog?.querySelector(':scope > p')?.remove();
  const upload = catalog?.querySelector('button[onclick*="openCatalogImport"]');
  const incoming = catalog?.querySelector('button[onclick*="openIncomingPrices"]');
  const historical = root.querySelector('[data-open="price-history"]');
  const more = [incoming, historical].filter(Boolean);
  if (more.length) catalog?.insertBefore(moreMenu(more), upload?.nextSibling || null);
  const material = catalog?.querySelector('#catalogQuery')?.closest('label');
  const specification = catalog?.querySelector('#catalogSpecification')?.closest('label');
  const search = catalog?.querySelector('button[onclick*="searchCatalog"]');
  if (material && specification && search) {
    const row = document.createElement('div');
    row.className = 'catalog-search';
    row.append(material, specification, search);
    catalog.insertBefore(row, catalog.querySelector('#catalogResults'));
  }
  const memory = document.createElement('button');
  memory.className = 'btn secondary';
  memory.textContent = 'Память цен по всей базе';
  memory.onclick = () => showView('comparison');
  catalog?.append(memory);
  root.querySelectorAll('.panel-title p').forEach(p => { if (p.textContent.includes('оплаченные цены')) p.remove(); });
  bindOpeners();
};

const fullRenderDocuments = renderDocuments;
renderDocuments = function() {
  fullRenderDocuments();
  const root = $('#documents');
  root.querySelectorAll('.file-cell small.muted').forEach(node => node.remove());
  const title = [...root.querySelectorAll('.panel-title p')].find(p => p.textContent.includes('SHA-256'));
  if (title) title.textContent = 'Файлы до 100 МБ';
  const flow = root.querySelector('.doc-flow');
  if (flow && !state.documentFlow?.suggestion && !state.documentFlow?.lot) {
    const wrapper = document.createElement('details');
    wrapper.className = 'optional-document-flow';
    wrapper.innerHTML = '<summary>Создать закупку из проекта</summary>';
    flow.replaceWith(wrapper);
    wrapper.append(flow);
  }
};

const fullRender = render;
render = function() {
  fullRender();
  const extra = $('#nav').querySelector('button:not([data-view])');
  if (extra) document.querySelector('.nav-more').append(extra);
};

const fullShowView = showView;
showView = function(name) {
  if ((name === 'admin' || name === 'templates') && state.role !== 'admin') return toast('Раздел доступен администратору', true);
  fullShowView(name);
  if (name === 'admin') {
    $('#admin').innerHTML = '<section class="panel"><h2>Настройки и аудит</h2><button class="btn secondary" onclick="editApprovalPolicy()">Правила согласования</button><div id="adminAudit">Загрузка журнала…</div></section>';
    api('/api/audit?limit=50').then(rows => {
      if (state.view !== 'admin') return;
      $('#adminAudit').innerHTML = `<details><summary>Технический аудит</summary><pre>${esc(JSON.stringify(rows, null, 2))}</pre></details>`;
    }).catch(error => { if (state.view === 'admin') $('#adminAudit').textContent = error.message; });
  }
};

document.querySelectorAll('.nav-more button[data-view]').forEach(button => {
  button.onclick = () => showView(button.dataset.view);
});
const initialExtra = $('#nav').querySelector('button:not([data-view])');
if (initialExtra) document.querySelector('.nav-more').append(initialExtra);
