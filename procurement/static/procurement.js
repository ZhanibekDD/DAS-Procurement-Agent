'use strict';
const purchasing={lot:null,preview:null,request:null,epoch:0,catalog:null,portfolio:null,lastSendError:null,lastSendOutcome:null,lastSentCount:0};

function procurementMailOutcome(outbox,campaign){
 const ids=(campaign?.messages||[]).map(m=>m.id);
 if(!ids.length)return {kind:'unknown',sent:0};
 const messages=ids.map(id=>outbox.find(m=>m.id===id));
 const sent=messages.filter(m=>m?.delivery?.status==='sent'&&m.delivery.accepted_at).length;
 if(messages.some(m=>!m||['unknown','queued','sending'].includes(m.delivery?.status)||['queued','sending'].includes(m.status)
   ||(m.status==='failed'&&!m.delivery)))return {kind:'unknown',sent};
 if(sent===ids.length)return {kind:'sent',sent};
 return {kind:'failed',sent};
}
const procurementStages=['Черновик','Запрос отправлен','Получены цены','Сравнение','Поставщик выбран','Заказ'];
pages.lots=['Закупки','Загрузите файл или откройте закупку, проверьте запрос и отправьте его.'];
pages.rfq=pages.lots;
pages.projects=['Портфель проектов','Исходные данные, материалы, закупки, цены и бюджет.'];
pages.pricebook=['Прайсы поставщиков','История предложений. Надёжность и индекс цены — независимые показатели.'];
const procurementShowView=showView;
showView=function(name){procurementShowView(name);if(['rfq','tender'].includes(name))document.querySelector('#nav [data-view="lots"]')?.classList.add('active')};
function procurementStage(l){return ({rfq_sent:1,quotes_received:2,comparison:3,awarded:4,ordered:5})[l.display_status||l.status]||0}
function invalidateProcurement(){purchasing.epoch++;purchasing.preview=null;purchasing.request=null;$('#procurementPreview')&&( $('#procurementPreview').innerHTML='<p>Выбор изменён. Обновите предпросмотр.</p>');$('#procurementPreviewBtn')?.classList.remove('secondary')}
function selectedRfqRequest(){return {supplier_ids:[...document.querySelectorAll('[name="procurementSupplier"]:checked')].map(n=>Number(n.value)),
 item_ids:[...document.querySelectorAll('[name="procurementItem"]:checked')].map(n=>Number(n.value)),channel:'email',template_code:'rfq-email'};}
function filteredProcurementMessages(lotId){return state.outbox.filter(m=>Number(m.lot_id)===Number(lotId))}
async function approveProcurementMessage(id){try{await launchJson(`/api/procurement/outbox/${id}/approve`,'POST',{confirmed:true});await loadAll();await openLot(state.selectedLot);toast('Запрос согласован')}catch(e){toast(e.message,true)}}
async function sendSavedProcurementMessage(id){try{const p=await api(`/api/outbox?lot_id=${state.selectedLot}`);const m=p.find(r=>r.id===id);if(!m||m.lot_id!==state.selectedLot)throw new Error('Запрос не принадлежит выбранной закупке');const snap=await api(`/api/procurement/campaigns/${m.campaign_id}/snapshot`);const r=await launchJson(`/api/procurement/outbox/${id}/send`,'POST',{lot_id:state.selectedLot,snapshot_sha256:snap.snapshot_sha256,confirmed:true});const notice=confirmedMailNotice(r);purchasing.lastSendError=null;await loadAll();await openLot(state.selectedLot);toast(notice,!!r.warning)}catch(e){purchasing.lastSendError=e.message;await loadAll();await openLot(state.selectedLot);toast(e.message,true)}}
const procurementLots=renderLots;
renderLots=function(){procurementLots();$('#lots').insertAdjacentHTML('afterbegin',`<section class="panel"><h2>Закупочный процесс</h2><p>${procurementStages.map(esc).join(' → ')}</p><p>Старые заявки и запросы сохранены с прежними ID. Откройте закупку для выбора позиций и предпросмотра.</p><button class="btn secondary" onclick="editApprovalPolicy()">Правила согласования</button></section>`)};
const procurementOpenLot=openLot;
openLot=async function(id){invalidateProcurement();const epoch=purchasing.epoch;state.selectedLot=Number(id);purchasing.lot=null;
 const inline=state.view==='lots';
 try{const [lot,matches]=await Promise.all([api(`/api/lots/${id}`),api(`/api/lots/${id}/supplier-matches`)]);if(epoch!==purchasing.epoch||state.selectedLot!==Number(id))return;
 purchasing.lot=lot;state.matches=matches;showView(inline?'lots':'rfq');
 if(inline){const host=$('#quickReview'),rfq=$('#rfq');host.replaceChildren(...rfq.childNodes);
   const heading=host.querySelector(':scope > section.panel');heading?.querySelector('button')?.remove();
   heading?.querySelector('select')?.remove();host.scrollIntoView({block:'start'});}
 if(document.querySelectorAll('[name="procurementSupplier"]:checked').length)await previewProcurement();
 }catch(e){toast(e.message,true)}};
renderRfq=function(){
 const lot=purchasing.lot?.id===state.selectedLot?purchasing.lot:null;
 const options=state.lots.map(l=>`<option value="${Number(l.id)}" ${Number(l.id)===state.selectedLot?'selected':''}>#${l.id} · ${esc(l.title)}</option>`).join('');
 $('#rfq').innerHTML=`<section class="panel"><button class="btn secondary" onclick="showView('lots')">← Все закупки</button><h2>${lot?esc(lot.title):'Выберите закупку'}</h2><select id="procurementLot" aria-label="Закупка"><option value="">Выберите лот</option>${options}</select><p>${procurementStages.map(esc).join(' → ')}</p></section>${lot?`<div class="layout"><section class="panel"><h3>Позиции лота #${lot.id}</h3><p>Источник запроса — только выбранный immutable ID и его позиции.</p>${lot.items.map(i=>`<label style="display:block;padding:8px"><input type="checkbox" name="procurementItem" value="${i.id}" checked> ${esc(i.name)} — ${esc(i.quantity)} ${esc(i.unit)}<small> ${esc(i.specification)}</small></label>`).join('')}<h3>Поставщики</h3>${state.matches.map(s=>`<label style="display:block;padding:8px"><input type="checkbox" name="procurementSupplier" value="${s.id}" ${s.auto_select?'checked':''}> ${esc(s.name)} · ${esc(s.email||'почта не указана')} · надёжность ${Number(s.rating).toFixed(1)} / 5</label>`).join('')||'<p>Нет подходящих поставщиков этого кластера.</p>'}<button class="btn secondary" id="procurementPreviewBtn" onclick="previewProcurement()">Обновить предпросмотр</button><div id="procurementPreview"></div></section><section class="panel"><h3>Запросы только этой закупки</h3>${filteredProcurementMessages(lot.id).map(m=>`<article class="message-card"><p>#${m.id} · ${esc(m.supplier_name)} · ${esc(m.status)}</p><details><summary>История запроса</summary>${messagePreview(m)}</details></article>`).join('')||'<p>Запросов ещё нет.</p>'}<button class="btn secondary" onclick="openProcurementComparison(${lot.id})">Цены и сравнение</button><button class="btn secondary" onclick="openModal('quote')">Добавить полученное КП</button></section></div>`:''}`;
 $('#procurementLot').onchange=e=>{const id=Number(e.target.value);invalidateProcurement();state.selectedLot=id||null;purchasing.lot=null;if(id)openLot(id);else renderRfq()};
 document.querySelectorAll('[name="procurementItem"],[name="procurementSupplier"]').forEach(el=>el.onchange=()=>{
   invalidateProcurement();if(selectedRfqRequest().supplier_ids.length&&selectedRfqRequest().item_ids.length)previewProcurement();
 });
};
async function previewProcurement(){
 const lid=state.selectedLot,request=selectedRfqRequest();if(!request.item_ids.length||!request.supplier_ids.length)return toast('Выберите позиции и поставщиков',true);
 invalidateProcurement();const epoch=purchasing.epoch;const button=$('#procurementPreviewBtn');button.disabled=true;
 try{const p=await launchJson(`/api/procurement/lots/${lid}/preview`,'POST',request);if(epoch!==purchasing.epoch||lid!==state.selectedLot)return;
 if(p.lot_id!==lid||p.items.some(i=>i.lot_id!==lid))throw new Error('Предпросмотр не соответствует выбранному лоту; отправка заблокирована');
 purchasing.preview=p;purchasing.request=request;
 const outcome=purchasing.lastSendOutcome?.kind;
 const prefix=outcome==='unknown'?'Результат отправки не подтверждён: ':outcome==='sent'?'Почтовый сервер принял письмо: ':
   purchasing.lastSentCount?`Отправлено: ${purchasing.lastSentCount}. Остальные не отправлены: `:'Не отправлено: ';
 $('#procurementPreview').innerHTML=`<h3>Проверьте запрос перед отправкой</h3>${p.messages.map(m=>messagePreview(m)+`<p>Вложения: ${(m.attachments||[]).map(a=>esc(a.filename)).join(', ')||'нет'}</p>`).join('<hr>')}${p.approval_required?'<p>По правилу компании требуется согласование.</p>':''}${purchasing.lastSendError?`<p role="alert">${prefix}${esc(purchasing.lastSendError)}</p>`:''}${outcome==='unknown'||outcome==='sent'?'':`<button class="btn" id="procurementSend" onclick="sendProcurement()">${p.approval_required?'Подготовить для согласования':purchasing.lastSendError?'Повторить':'Отправить запрос КП'}</button>`}<p id="procurementSendStatus" role="status"></p>`;
 button.classList.add('secondary');
 }catch(e){toast(e.message,true)}finally{if(button.isConnected)button.disabled=false}
}
async function sendProcurement(){
 const p=purchasing.preview,request=purchasing.request,lid=state.selectedLot;
 if(!p||p.lot_id!==lid||JSON.stringify(request)!==JSON.stringify(selectedRfqRequest()))return toast('Обновите предпросмотр выбранного лота',true);
 const button=$('#procurementSend');button.disabled=true;
 const status=$('#procurementSendStatus');if(status)status.textContent='Отправляется…';
 let sentCount=0,campaign=null,attemptedSend=false;
 try{campaign=await launchJson(`/api/lots/${lid}/campaigns`,'POST',{...request,snapshot_sha256:p.snapshot_sha256,preview_sha256:p.preview_sha256});
 if(campaign.lot_id!==lid)throw new Error('ID запроса не совпал с закупкой');
 if(p.approval_required){toast('Черновики созданы. Требуется согласование по правилу.');}
 else{let warnings=[];for(const m of campaign.messages){attemptedSend=true;const r=await launchJson(`/api/procurement/outbox/${m.id}/send`,'POST',{lot_id:lid,snapshot_sha256:p.snapshot_sha256,confirmed:true});confirmedMailNotice(r);if(r.warning)warnings.push(r.warning);sentCount++;}purchasing.lastSendError=null;purchasing.lastSendOutcome=null;purchasing.lastSentCount=0;if(status)status.textContent='Отправлено: почтовый сервер принял '+sentCount+' письмо(а).';toast(warnings.length?`SMTP принял запросы: ${sentCount}. ${warnings[0]}`:`SMTP принял запросы: ${sentCount}; копии сохранены в «Отправленных». Доставка пока не подтверждена.`,!!warnings.length)}
 invalidateProcurement();await loadAll();await openLot(lid);
 }catch(e){let outcome={kind:'failed',sent:0};if(attemptedSend){outcome={kind:'unknown',sent:sentCount};try{outcome=procurementMailOutcome(await api(`/api/outbox?lot_id=${lid}`),campaign)}catch{}}
 purchasing.lastSendError=e.message;purchasing.lastSendOutcome=outcome;purchasing.lastSentCount=outcome.sent;
 if(status)status.textContent=outcome.kind==='unknown'?'Результат отправки не подтверждён. Повтор заблокирован; проверьте журнал.':
   outcome.kind==='sent'?'Отправлено: почтовый сервер принял письмо, но ответ интерфейсу не дошёл.':
   outcome.sent?`Отправлено: ${outcome.sent}. Остальные не отправлены: ${e.message}`:'Не отправлено: '+e.message;
 toast(status?.textContent||e.message,true);await loadAll();await openLot(lid)}finally{if(button.isConnected)button.disabled=false}
}
function procurementDecisionControls(lot,quotes){if(lot.status==='ordered')return '<p>Заказ уже зафиксирован. Повторный выбор поставщика недоступен.</p>';return quotes.map(q=>`<p>${esc(q.supplier_name)} <button class="btn secondary" onclick="chooseProcurement(${lot.id},${q.id},'awarded')">Выбрать поставщика</button> <button class="btn secondary" onclick="chooseProcurement(${lot.id},${q.id},'ordered')">Зафиксировать заказ</button></p>`).join('')}
async function openProcurementComparison(lid){state.selectedLot=lid;await loadComparison();showView('comparison');const quotes=await api(`/api/lots/${lid}/quotes`),lot=await api(`/api/lots/${lid}`);if(quotes.length&&lot.status!=='ordered')await launchJson(`/api/procurement/lots/${lid}/comparison`,'POST',{confirmed:true});$('#comparison').insertAdjacentHTML('beforeend',`<section class="panel"><h3>Решение сотрудника</h3>${procurementDecisionControls(lot,quotes)}</section>`)}
async function chooseProcurement(lid,qid,stage){try{await launchJson(`/api/procurement/lots/${lid}/decision`,'POST',{quote_id:qid,stage});await loadAll();await openLot(lid);toast(stage==='ordered'?'Заказ зафиксирован. Договор и оплата не выполнялись.':'Поставщик выбран')}catch(e){toast(e.message,true)}}
async function editApprovalPolicy(){try{const p=await api('/api/procurement/policy');launchModal('Правила согласования',`<p>Изменять правила может только администратор. При включённом пороге неизвестная сумма требует согласования.</p><label>Порог суммы (пусто — выключено)<input id="policyThreshold" value="${esc(p.amount_threshold||'')}"></label><label>Валюта<input id="policyCurrency" value="${esc(p.currency)}"></label><label><input id="policyStaff" type="checkbox" ${p.required_roles.includes('staff')?'checked':''}> Отдельное согласование для роли staff</label>`,'Сохранить',async()=>{try{await launchJson('/api/procurement/policy','PUT',{amount_threshold:$('#policyThreshold').value||null,currency:$('#policyCurrency').value,required_roles:$('#policyStaff').checked?['staff']:[]});$('#modal').close();toast('Правило сохранено')}catch(e){toast(e.message,true)}})}catch(e){toast(e.message,true)}}

const procurementProjects=renderProjects;
renderProjects=function(){procurementProjects();document.querySelectorAll('.project-card').forEach((card,n)=>{const p=state.projects[n];if(p)card.querySelector('.project-actions').insertAdjacentHTML('beforeend',`<button class="btn secondary small" onclick="openPortfolio(${p.id})">Карточка объекта</button>`)})};
async function openPortfolio(id){try{const data=await api(`/api/procurement/projects/${id}`);purchasing.portfolio=data;showView('projects');
 const sheetRows=sheet=>data.workbook_rows.filter(r=>r.sheet===sheet);
 const rowsMarkup=rows=>`<div class="table-wrap"><table>${rows.map(r=>`<tr>${r.cells.map(c=>`<td>${esc(c)}</td>`).join('')}<td><span class="badge">${esc(r.status)}</span><br><a target="_blank" href="/api/procurement/documents/${r.document_id}/view?sheet=${encodeURIComponent(r.sheet)}">${esc(r.filename)} · ${esc(r.sheet)} · строка ${r.row_number}</a></td></tr>`).join('')}</table></div>`;
 $('#projects').innerHTML=`<section class="panel"><button class="btn secondary" onclick="renderProjects()">← Портфель</button><h2>${esc(data.project.name)}</h2><p>${esc(data.project.delivery_address)}</p><label>Расчёт материалов XLSX<input id="portfolioFile" type="file" accept=".xlsx"></label><button class="btn" onclick="previewPortfolio(${id})">Предпросмотр расчёта</button><div id="portfolioPreview"></div><p>Каждая строка хранит документ, SHA, лист и номер строки. Формулы не заменяются придуманными значениями.</p></section>
 ${['Итоги','Исходные','Спецификации','Источники'].map(s=>`<section class="panel"><h3>${s==='Исходные'?'Исходные данные':s}</h3>${sheetRows(s).length?rowsMarkup(sheetRows(s)):'<p>Источник ещё не загружен.</p>'}</section>`).join('')}
 <section class="panel"><h3>Документы</h3>${data.documents.map(d=>documentActions(d)).join('')}</section><section class="panel"><h3>Материалы / объёмы</h3><table>${data.materials.map(i=>`<tr><td>${esc(i.name)}</td><td>${esc(i.quantity)} ${esc(i.unit)}</td><td>${esc(i.source_reference||'Источник не определён')}</td><td>${i.source_document_id?`Документ #${i.source_document_id}, лист ${i.source_page||'не определён'}`:'Не определено'}</td></tr>`).join('')}</table></section>
 <section class="panel"><h3>Закупки</h3>${lotsTable(data.procurements)}</section><section class="panel"><h3>Полученные цены</h3>${data.quotes.map(q=>`<p>${esc(q.supplier_name)} · ${q.items.length} позиций · ${esc(q.currency)}</p>`).join('')||'<p>Цены ещё не получены.</p>'}</section><section class="panel"><h3>Бюджет</h3>${data.budget.map(b=>`<p>${esc(b.amount)} ${esc(b.currency)}</p>`).join('')||'<p>Бюджет не определён — не считается нулём.</p>'}<label>Сумма<input id="portfolioBudget"></label><label>Валюта<input id="portfolioCurrency" value="RUB"></label><button class="btn secondary" onclick="savePortfolioBudget(${id})">Сохранить бюджет</button></section>`;
 }catch(e){toast(e.message,true)}}
async function previewPortfolio(id){const file=$('#portfolioFile').files[0];if(!file)return toast('Выберите XLSX',true);try{const fd=new FormData();fd.append('file',file);const preview=await api(`/api/procurement/projects/${id}/workbook`,{method:'POST',body:fd});$('#portfolioPreview').innerHTML=`<p>Строк ${preview.rows.length}; листы: ${[...new Set(preview.rows.map(r=>r.sheet))].map(esc).join(', ')}</p><details><summary>Все строки и статусы</summary>${preview.rows.map(r=>`<p>${esc(r.sheet)}:${r.row} · ${esc(r.cells.join(' | '))} · ${esc(r.status)}</p>`).join('')}</details><label><input id="portfolioConfirmed" type="checkbox"> Проверены исходный документ и статусы</label><button class="btn" id="portfolioApply">Сохранить в проект</button>`;$('#portfolioApply').onclick=async()=>{if(!$('#portfolioConfirmed').checked)return toast('Подтвердите источник',true);fd.set('confirmed','true');fd.set('expected_sha256',preview.sha256);try{await api(`/api/procurement/projects/${id}/workbook`,{method:'POST',body:fd});await loadAll();await openPortfolio(id);toast('Расчёт сохранён с источниками')}catch(e){toast(e.message,true)}}}catch(e){toast(e.message,true)}}
async function savePortfolioBudget(id){try{await launchJson(`/api/procurement/projects/${id}/budget`,'PUT',{amount:$('#portfolioBudget').value,currency:$('#portfolioCurrency').value});await openPortfolio(id)}catch(e){toast(e.message,true)}}
function documentActions(d){return `<p>${esc(d.filename)} <button class="btn secondary small" onclick="viewProcurementDocument(${Number(d.id)})">Просмотр</button> <a href="/api/launch/documents/${Number(d.id)}/download">Скачать оригинал</a></p>`}
function viewProcurementDocument(id){const doc=state.documents.find(d=>Number(d.id)===id)||purchasing.portfolio?.documents.find(d=>d.id===id);launchModal(doc?.filename||'Документ',`<p><a href="/api/launch/documents/${id}/download">Скачать оригинал</a></p><iframe title="Просмотр документа" src="/api/procurement/documents/${id}/view" style="width:100%;height:65vh;border:1px solid #ddd"></iframe>`,'Закрыть',()=>$('#modal').close())}
const procurementDocuments=renderDocuments;
renderDocuments=function(){procurementDocuments();$('#documents').insertAdjacentHTML('beforeend',`<section class="panel"><h3>Просмотр без скачивания</h3>${state.documents.map(documentActions).join('')}</section>`)};
const procurementPricebook=renderPricebook;
renderPricebook=function(){procurementPricebook();$('#pricebook').insertAdjacentHTML('afterbegin',`<section class="panel"><h2>База прайсов поставщиков</h2><p>Каждый импорт — новая запись истории. Дорогой товар не снижает надёжность поставщика.</p><button class="btn" onclick="openCatalogImport()">Загрузить XLSX / CSV / PDF</button><label>Материал<input id="catalogQuery" placeholder="ФБС 24.4.6"></label><label>Характеристики<input id="catalogSpecification"></label><button class="btn secondary" onclick="searchCatalog()">Найти поставщиков</button><div id="catalogResults"></div></section>`)};
async function searchCatalog(){try{const rows=await api('/api/procurement/catalog?q='+encodeURIComponent($('#catalogQuery').value)+'&specification='+encodeURIComponent($('#catalogSpecification').value));$('#catalogResults').innerHTML=`<p>${rows.length} записей истории. Медиана только по совместимым единицам, валюте, НДС, региону, доставке и партии.</p><div class="table-wrap"><table><tr><th>Товар / характеристики</th><th>Поставщик</th><th>Цена / НДС</th><th>Доставка / регион / партия</th><th>Дата / срок</th><th>Надёжность</th><th>Индекс цены</th></tr>${rows.map(r=>`<tr><td>${esc(r.item_name)}<br>${esc(r.specification)} · ${esc(r.category)}</td><td>${esc(r.supplier_name)}</td><td>${esc(r.unit_price)} ${esc(r.currency)} / ${esc(r.unit)}<br>${esc(r.vat||'НДС не указан')}</td><td>${esc(r.delivery||'не определено')}<br>${esc(r.region)} · ${esc(r.minimum_batch||'партия не указана')}</td><td>${esc(r.price_date)}<br>${esc(r.valid_until||'срок не определён')} · ${r.current?'актуальная запись':'история'}</td><td>${Number(r.reliability).toFixed(1)} / 5</td><td>${r.price_index_pct==null?'Недостаточно сопоставимых данных':r.price_index_pct===0?'Цена на уровне медианы':esc(`Цена ${r.price_index_pct>0?'выше':'ниже'} текущей медианы на ${Math.abs(r.price_index_pct)}%`)}</td></tr>`).join('')}</table></div>`}catch(e){toast(e.message,true)}}
function openCatalogImport(){launchModal('Импорт прайса',`<p>Для таблицы сопоставьте колонки. Для PDF сначала извлечение, затем ручная проверка реквизитов и строк.</p><input id="catalogFile" type="file" accept=".xlsx,.csv,.pdf"><label>Лист<input id="catalogSheet"></label><label>Строка заголовков<input id="catalogHeader" type="number" value="1" min="1" max="100"></label><div id="catalogMapping"></div><div id="catalogImportPreview"></div>`,'Предпросмотр',previewCatalogImport)}
async function previewCatalogImport(){const file=$('#catalogFile').files[0];if(!file)return toast('Выберите прайс',true);try{const fd=new FormData();fd.append('file',file);fd.append('sheet',$('#catalogSheet').value);fd.append('header_row',$('#catalogHeader').value);const mapping={};document.querySelectorAll('[data-catalog-map]').forEach(e=>{if(e.value!=='')mapping[e.dataset.catalogMap]=Number(e.value)});if(Object.keys(mapping).length)fd.append('mapping',JSON.stringify(mapping));const p=await api('/api/procurement/catalog/preview',{method:'POST',body:fd});if(p.requires_review){showPdfPricePreview(p);return}
 const fields={item_name:'Товар',specification:'Характеристики',category:'Категория',unit:'Единица',unit_price:'Цена',currency:'Валюта',vat:'НДС',delivery:'Доставка',region:'Регион',minimum_batch:'Минимальная партия',price_date:'Дата прайса',valid_until:'Действует до',supplier_name:'Поставщик',tax_id:'ИНН',email:'Email',phone:'Телефон'};
 $('#catalogMapping').innerHTML=Object.entries(fields).map(([key,label])=>`<label>${label}<select data-catalog-map="${key}"><option value="">Не выбрано</option>${p.headers.map((h,n)=>`<option value="${n}" ${p.mapping[key]===n?'selected':''}>${esc(h)}</option>`).join('')}</select></label>`).join('')+'<button type="button" class="btn secondary" onclick="previewCatalogImport()">Повторить с выбранными колонками</button>';
 $('#catalogImportPreview').innerHTML=`<p>Корректных строк ${p.rows.length}; ошибок ${p.errors.length}</p>${p.errors.map(e=>`<p>Строка ${e.row}: ${esc(e.error)}</p>`).join('')}<details><summary>Все проверяемые строки</summary>${p.rows.map(r=>`<p>${esc(JSON.stringify(r))}</p>`).join('')}</details><label><input id="catalogConfirmed" type="checkbox"> Проверены поставщик, цены, НДС, даты и доставка</label>`;$('#modalSubmit').textContent='Импортировать';$('#modalSubmit').onclick=async()=>{if(!$('#catalogConfirmed').checked)return toast('Подтвердите строки',true);try{const r=await launchJson(`/api/procurement/catalog/${p.preview_id}/apply`,'POST',{confirmed:true});$('#modal').close();await loadAll();showView('pricebook');toast(`Прайс: добавлено ${r.added}, пропущено ${r.skipped}; ошибок ${r.errors.length}`)}catch(e){toast(e.message,true)}};
 for(const e of document.querySelectorAll('[data-catalog-map],#catalogFile,#catalogSheet,#catalogHeader'))e.onchange=()=>{$('#modalSubmit').textContent='Предпросмотр';$('#modalSubmit').onclick=previewCatalogImport;$('#catalogImportPreview').textContent='Данные изменились. Повторите предпросмотр.'};
 }catch(e){toast(e.message,true)}}
const catalogFieldLabels={item_name:'Товар',specification:'Характеристики',category:'Категория',unit:'Единица',unit_price:'Цена',currency:'Валюта',vat:'НДС',delivery:'Доставка',region:'Регион',minimum_batch:'Минимальная партия',price_date:'Дата прайса',valid_until:'Действует до',supplier_name:'Поставщик',tax_id:'ИНН',email:'Email',phone:'Телефон'};
let catalogPdfRows=[];
function catalogEditableRows(){return catalogPdfRows.map(r=>Object.fromEntries(Object.keys(catalogFieldLabels).map(k=>[k,String(r[k]??'')])));}
function catalogRowErrors(errors){
  const target=$('#catalogRowErrors');
  if(target)target.innerHTML='<p><b>Прайс пока не сохранён. Исправьте указанные строки:</b></p>'+errors.map(e=>`<p>Строка ${Number(e.row)}: ${esc(e.error)}</p>`).join('');
  target?.scrollIntoView?.({block:'nearest'});
  toast(errors.map(e=>`Строка ${e.row}: ${e.error}`).slice(0,5).join('; '),true);
}
function catalogRequestError(message){
  const target=$('#catalogRowErrors');
  if(target){target.textContent='Прайс пока не сохранён: '+message;target.scrollIntoView?.({block:'nearest'});}
  toast(message,true);
}
function showPdfPricePreview(p){
  showInboxPriceRows({...p,filename:p.source_filename,confirm_rub:true});
  if($('#catalogMapping'))catalogCommonFields($('#catalogMapping'));
  $('#modalSubmit').textContent='Проверить строки';
  $('#modalSubmit').onclick=async()=>{
    const button=$('#modalSubmit');button.disabled=true;
    try{
      const confirmed_rub=$('#catalogRubConfirmed')?.checked===true;
      const rows=catalogEditableRows();
      if(rows.some(r=>!r.currency)&&!confirmed_rub){catalogRowErrors([{row:1,error:'Подтвердите по оригиналу, что цены указаны в рублях'}]);return;}
      const checked=await launchJson(`/api/procurement/catalog/${p.preview_id}/review-pdf`,'POST',{rows,confirmed_rub});
      if(!checked.rows?.length){catalogRowErrors(checked.errors||[{row:1,error:'Нет подтверждённых цен'}]);return;}
      showConfirmedCatalog(checked);
    }catch(e){catalogRequestError(e.message)}finally{button.disabled=false}
  };
}
function showConfirmedCatalog(p){
  $('#catalogImportPreview').innerHTML=`<div id="catalogRowErrors" role="alert"></div><p>К сохранению: ${p.rows.length} строк с подтверждённой ценой. ${p.errors.length?`${p.errors.length} строк без проверенной цены или реквизитов не попадут в историю.`:'Ошибок нет.'}</p>${p.errors.length?`<details><summary>Строки, которые не будут сохранены (${p.errors.length})</summary>${p.errors.map(e=>`<p>Строка ${Number(e.row)}: ${esc(e.error)}</p>`).join('')}</details>`:''}<details><summary>Проверенные строки (${p.rows.length})</summary>${p.rows.map(r=>`<p>${esc(JSON.stringify(r))}</p>`).join('')}</details><label><input id="catalogConfirmed" type="checkbox"> Подтверждаю сохранение ${p.rows.length} строк; ${p.errors.length} строк с ошибками пропустить</label>`;
  $('#modalSubmit').textContent='Сохранить проверенные цены';
  $('#modalSubmit').onclick=async()=>{
    if(!$('#catalogConfirmed').checked)return catalogRequestError('Подтвердите, какие строки будут сохранены и какие останутся без цены');
    try{const r=await launchJson(`/api/procurement/catalog/${p.preview_id}/apply`,'POST',{confirmed:true});$('#modal').close();await loadAll();showView('pricebook');toast(`Добавлено ${r.added}, повторов ${r.skipped}; не сохранено строк с ошибками ${r.errors.length}`)}
    catch(e){catalogRequestError(e.message)}
  };
}
function openIncomingPrices(){launchModal('Прайсы из входящей почты',`<p>Сохраните исходное входящее письмо как EML и загрузите сюда. Вложения XLSX/CSV/PDF разбираются отдельно; отправитель письма не считается подтверждённым поставщиком.</p><input id="incomingPriceMail" type="file" accept=".eml"><div id="incomingPriceChoices"></div><div id="catalogImportPreview"></div>`,'Предпросмотр',async()=>{const file=$('#incomingPriceMail').files[0];if(!file)return toast('Выберите EML',true);try{const fd=new FormData();fd.append('file',file);const result=await api('/api/procurement/catalog/incoming-mail',{method:'POST',body:fd});$('#incomingPriceChoices').innerHTML=result.previews.map((p,n)=>`<button type="button" id="incomingPrice${n}" class="btn secondary">Вложение ${n+1}</button>`).join('');result.previews.forEach((p,n)=>{$('#incomingPrice'+n).onclick=()=>p.requires_review?showPdfPricePreview(p):showConfirmedCatalog(p)});toast('Вложения извлечены; выберите и проверьте каждое')}catch(e){toast(e.message,true)}})}
const catalogWithMail=renderPricebook;
renderPricebook=function(){catalogWithMail();$('#pricebook .panel').insertAdjacentHTML('beforeend','<button class="btn secondary" onclick="openIncomingPrices()">Из входящего письма EML</button>')};
const procurementModal=showModalForm;
showModalForm=function(type){procurementModal(type);if(type==='document'&&$('#dFile'))$('#dFile').accept='.pdf,.docx,.xlsx,.csv,.png,.jpg,.jpeg'};
const procurementRfqWithRules=renderRfq;
renderRfq=function(){procurementRfqWithRules();const messages=filteredProcurementMessages(state.selectedLot);document.querySelectorAll('#rfq .message-card').forEach((card,n)=>{const m=messages[n];if(!m)return;card.insertAdjacentHTML('beforeend',mailJournal(m));const b=document.createElement('button');b.className='btn secondary small';if(m.status==='draft'){b.textContent='Согласовать по правилу (администратор)';b.onclick=()=>approveProcurementMessage(m.id)}else if(m.status==='approved'||m.delivery?.retry_allowed){b.textContent=m.delivery?.retry_allowed?'Безопасный повтор':'Отправить согласованный запрос';b.onclick=()=>{if(confirm('Отправить этот подтверждённый запрос поставщику?'))sendSavedProcurementMessage(m.id)}}else return;card.appendChild(b)})};
const procurementLotsWithStages=renderLots;
renderLots=function(){procurementLotsWithStages();const select=$('#lotStatusFilter');if(select){select.innerHTML='<option value="">Все статусы</option>'+[['draft','Черновик'],['rfq_draft','Черновик запроса'],['rfq_queued','В очереди'],['rfq_sending','Отправляется'],['rfq_failed','Ошибка отправки'],['rfq_unconfirmed','Отправка не подтверждена'],['rfq_partial','Отправлено не всем'],['rfq_sent','Запрос отправлен'],['quotes_received','Получены цены'],['comparison','Сравнение'],['awarded','Поставщик выбран'],['ordered','Заказ']].map(([k,v])=>`<option value="${k}">${v}</option>`).join('')}};
const procurementOverview=renderOverview;
renderOverview=function(){procurementOverview();const pipeline=document.querySelector('#overview .pipeline');if(pipeline)pipeline.innerHTML=procurementStages.map((name,n)=>`<div class="pipe-step"><b>${String(n+1).padStart(2,'0')} · ${esc(name)}</b><span>${n===1?'Предпросмотр → Отправить запрос':n===3?'Надёжность отдельно от индекса цены':esc(name)}</span></div>`).join('')};

// Corporate mailbox content is private until an administrator verifies a price.
const pricebookWithInbox=renderPricebook;
renderPricebook=function(){
  pricebookWithInbox();
  if(state.role!=='admin')return;
  $('#pricebook').insertAdjacentHTML('afterbegin','<section class="panel"><h2>КП из почты</h2><p>Вложения получаем автоматически. Компании и цены сохраняются только после вашей проверки.</p><button class="btn secondary" onclick="loadIncomingPrices()">Обновить</button><div id="mailPriceInbox" aria-live="polite">Загрузка…</div></section>');
  loadIncomingPrices();
};
async function loadIncomingPrices(before=null){
  const target=$('#mailPriceInbox');if(!target)return;
  try{
    const result=await api('/api/procurement/inbox'+(before?'?before='+encodeURIComponent(before):''));
    if(target!==$('#mailPriceInbox'))return;
    const connections=result.connections.map(c=>{
      const stale=!c.checked_at||Date.now()-Date.parse(c.checked_at)>180000;
      return `<p ${c.error||stale?'role="alert"':''}>${esc(c.error||(stale?'Автоматическая проверка почты задерживается.':'Почта проверяется автоматически.'))} Последняя проверка: ${esc(c.checked_at?new Date(c.checked_at).toLocaleString('ru-RU'):'ещё не выполнена')}${c.next_uid>c.last_uid+1?' · Обрабатываем предыдущие письма.':''}</p>`;
    }).join('')||'<p>Автоматический сбор ещё не запущен.</p>';
    target.innerHTML=connections+(result.messages.length?result.messages.map(m=>`<article class="message-card"><h3>${esc(m.subject||'Без темы')}</h3><p>${esc(m.sender||'Отправитель не определён')} · ${esc(new Date(m.received_at).toLocaleString('ru-RU'))}</p>${m.error?`<p role="alert">${esc(m.error)}</p>`:''}${m.status==='duplicate'?'<p>Повторное письмо. Повторный импорт не требуется.</p>':''}${m.attachments.length?m.attachments.map(a=>`<p>${esc(a.filename)} · ${a.applied?'Цены сохранены':'Требует проверки'} <button class="btn secondary" data-inbox-attachment="${esc(a.id)}">${a.applied?'Открыть исходник':'Проверить КП'}</button></p>`).join(''):'<p>Поддерживаемых прайсов для проверки нет.</p>'}</article>`).join(''):'<p>Входящих писем пока нет. Новые PDF, XLSX и CSV появятся здесь после проверки ящика.</p>')+(result.next_before?'<button class="btn secondary" id="olderIncomingPrices">Предыдущие письма</button>':'');
    target.querySelectorAll('[data-inbox-attachment]').forEach(b=>b.onclick=()=>openInboxAttachment(b.dataset.inboxAttachment));
    if(result.next_before)$('#olderIncomingPrices').onclick=()=>loadIncomingPrices(result.next_before);
  }catch(e){if(target===$('#mailPriceInbox'))target.textContent=e.message;}
}
async function openInboxAttachment(id){
  try{
    let p=await api('/api/procurement/inbox/attachments/'+encodeURIComponent(id));
    let recognitionError='';
    if((p.recognition_version||0)<2&&!p.reviewed){
      toast('Распознаём исходное КП. Это может занять до нескольких минут.');
      try{p=await launchJson('/api/procurement/inbox/attachments/'+encodeURIComponent(id)+'/recognize','POST',{});}
      catch(e){recognitionError='Повторное распознавание не выполнено: '+e.message;
        try{p=await api('/api/procurement/inbox/attachments/'+encodeURIComponent(id));}catch{}
      }
    }
    if(p.applied){launchModal('Исходное КП',`<p>Цены из этого КП уже сохранены. Повторный импорт не выполняется.</p><a href="/api/procurement/inbox/attachments/${encodeURIComponent(id)}/original" target="_blank" rel="noopener">Открыть оригинал: ${esc(p.filename)}</a>`,'Закрыть',()=>$('#modal').close());return;}
    launchModal('Проверка входящего КП',`<p><a href="/api/procurement/inbox/attachments/${encodeURIComponent(id)}/original" target="_blank" rel="noopener">Открыть оригинал: ${esc(p.filename)}</a></p><p>Проверьте компанию по документу. Отправитель письма не подтверждает ИНН и реквизиты.</p><div id="inboxCommon"></div><div id="catalogImportPreview"></div><label><input id="inboxVerified" type="checkbox"> Реквизиты сверены с оригиналом, цены указаны в рублях</label>`,'Проверить строки',()=>{});
    showInboxPriceRows(p);
    if(recognitionError)$('#catalogRowErrors').textContent=recognitionError;
    catalogCommonFields($('#inboxCommon'));
    $('#modalSubmit').onclick=async()=>{
      if(!$('#inboxVerified').checked)return toast('Подтвердите реквизиты по оригиналу и цены в рублях',true);
      const button=$('#modalSubmit');button.disabled=true;
      try{
        const rows=catalogEditableRows().map(r=>({...r,currency:r.currency||'RUB'}));
        const checked=await launchJson('/api/procurement/inbox/attachments/'+encodeURIComponent(id)+'/review','POST',{rows,confirmed_source:true});
        if(checked.requires_correction){
          catalogRowErrors(checked.errors);return;
        }
        showConfirmedCatalog(checked);
      }catch(e){catalogRequestError(e.message)}finally{button.disabled=false}
    };
  }catch(e){toast(e.message,true)}
}

function catalogCommonFields(target){
  const common=['supplier_name','tax_id','email','phone','region','price_date','valid_until','vat','delivery'];
  target.innerHTML='<details><summary>Заполнить общие реквизиты в пустых полях всех строк</summary>'+common.map(k=>`<label>${esc(catalogFieldLabels[k])}<input data-inbox-common="${k}" ${['price_date','valid_until'].includes(k)?'placeholder="ГГГГ-ММ-ДД"':''}></label>`).join('')+'<button type="button" class="btn secondary" id="fillInboxCommon">Применить к пустым полям</button></details>';
  $('#fillInboxCommon').onclick=()=>{
    document.querySelectorAll('[data-inbox-common]').forEach(input=>{
      if(!input.value.trim())return;
      document.querySelectorAll('[data-price-field]').forEach(cell=>{
        if(cell.dataset.priceField===input.dataset.inboxCommon&&!cell.value.trim()){
          cell.value=input.value.trim();cell.dispatchEvent(new Event('input'));
        }
      });
    });
  };
}
function showInboxPriceRows(p){
  catalogPdfRows=p.rows.map(r=>({...Object.fromEntries(Object.keys(catalogFieldLabels).map(k=>[k,String(r[k]??'')])),review_warning:r.review_warning||''}));
  const main=['item_name','unit_price','unit','specification'];
  const remaining=Object.keys(catalogFieldLabels).filter(k=>!main.includes(k)&&k!=='currency');
  const field=(r,n,k)=>`<label style="display:flex;flex-direction:column;min-width:0">${esc(catalogFieldLabels[k])}<input style="box-sizing:border-box;width:100%;min-width:0" data-price-row="${n}" data-price-field="${k}" value="${esc(r[k])}" ${k==='unit_price'&&!r[k]?'aria-invalid="true"':''}></label>`;
  const draw=()=>{
    $('#catalogImportPreview').innerHTML=`<div id="catalogRowErrors" role="alert"></div><p>Позиций: ${catalogPdfRows.length}. Цены — в рублях. Проверьте данные по оригиналу.</p>${(p.errors||[]).length?`<div role="alert">${p.errors.map(e=>`<p>${esc(e)}</p>`).join('')}</div>`:''}${(p.review_lines||[]).length?`<details><summary>Все строки распознанного документа — проверьте полноту</summary><pre style="white-space:pre-wrap">${esc(p.review_lines.map(l=>`Страница ${l.page}, строка ${l.line}: ${l.text}`).join('\n'))}</pre></details>`:''}${catalogPdfRows.map((r,n)=>`<fieldset style="border:1px solid #dbe2e5;border-radius:12px;margin:12px 0;padding:12px;min-width:0"><legend>Позиция ${n+1}</legend><div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px">${main.map(k=>field(r,n,k)).join('')}</div>${r.review_warning?`<p role="note">${esc(r.review_warning)}</p>`:''}${r.currency!=='RUB'?`<p role="alert">В исходнике другая или неизвестная валюта. Автоматическая конвертация не выполняется.</p>`:''}<details><summary>Поставщик и условия</summary><div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-top:12px">${remaining.map(k=>field(r,n,k)).join('')}</div></details><button type="button" class="btn secondary small" data-price-remove="${n}">Убрать строку</button></fieldset>`).join('')}<button type="button" class="btn secondary" id="pdfPriceAdd">Добавить строку</button>`;
    if(p.confirm_rub)$('#catalogImportPreview').insertAdjacentHTML('beforeend','<label><input id="catalogRubConfirmed" type="checkbox"> Подтверждаю по оригиналу: цены указаны в рублях, пересчёт валют не требуется</label>');
    document.querySelectorAll('[data-price-row]').forEach(e=>e.oninput=()=>{catalogPdfRows[Number(e.dataset.priceRow)][e.dataset.priceField]=e.value});
    document.querySelectorAll('[data-price-remove]').forEach(e=>e.onclick=()=>{catalogPdfRows.splice(Number(e.dataset.priceRemove),1);draw()});
    $('#pdfPriceAdd').onclick=()=>{catalogPdfRows.push({...Object.fromEntries(Object.keys(catalogFieldLabels).map(k=>[k,''])),currency:'RUB'});draw()};
  };
  draw();
}
