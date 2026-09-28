'use strict';
/* Presentation only: the server remains the authority for ACL, lot contents and SMTP status. */
Object.assign(mailStatus, {
  draft: 'Черновик', approved: 'Готов к отправке', queued: 'Готов к отправке',
  sending: 'Отправляется', sent: 'Отправлен', failed: 'Ошибка отправки',
  unknown: 'Результат отправки не подтверждён'
});
const statusNames = {
  draft:'Черновик', rfq_draft:'Черновик', rfq_sent:'Запрос отправлен',
  queued:'Готов к отправке', approved:'Готов к отправке', sending:'Отправляется',
  sent:'Отправлен', failed:'Ошибка отправки', unknown:'Результат отправки не подтверждён',
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
    ${delivery.error ? `<p role="alert">${esc(delivery.error)}</p>` : ''}
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
    } else showDeletedSuppliers();
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

const fullRenderLots = renderLots;
renderLots = function() {
  fullRenderLots();
  const root = $('#lots');
  root.querySelectorAll(':scope > section.panel').forEach(panel => {
    if (panel.querySelector('h2')?.textContent === 'Закупочный процесс') panel.remove();
  });
  const create = root.querySelector('[data-open="lot"]');
  if (create) create.textContent = 'Новая закупка';
  root.querySelectorAll('#lotsTable tbody td:first-child small.muted').forEach(node => node.remove());
  const excel = root.querySelector(':scope > button[onclick*="openLaunchImport"]');
  if (excel) {
    excel.classList.replace('btn', 'btn');
    excel.textContent = 'Создать из Excel или CSV';
    const title = root.querySelector('.panel-title');
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
  const upload = catalog?.querySelector('button[onclick*="openCatalogImport"]');
  const incoming = catalog?.querySelector('button[onclick*="openIncomingPrices"]');
  const historical = root.querySelector('[data-open="price-history"]');
  const more = [incoming, historical].filter(Boolean);
  if (more.length) catalog?.insertBefore(moreMenu(more), upload?.nextSibling || null);
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
