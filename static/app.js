import { BeeSmartApi, ApiError } from './api.js';

const $ = id => document.getElementById(id);
const api = new BeeSmartApi();
const roles = ['profile', 'history', 'tariffs'];
const fileLimits = { profile: 32 * 1024 ** 2, history: 16 * 1024 ** 2, tariffs: 256 * 1024 };
const limitLabels = { profile: 'До 32 МиБ', history: 'До 16 МиБ', tariffs: 'До 256 КиБ' };
const channelNames = { push: 'Push', sms: 'SMS', digital_ads: 'Digital', call: 'Звонок' };
const channelColors = { push: '#c9bafb', sms: '#ffda3d', digital_ads: '#90c9c0', call: '#343a40' };
const numberFormat = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 0 });
const decimalFormat = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 1 });
const usdFormat = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 6 });
const state = {
  overview: null, run: null, connected: false, source: 'upload', files: {},
  refreshing: false, submitting: false, uncertain: false, cooldownUntil: 0,
  pollTimer: null, pollBusy: false, pollPaused: false, revision: 0,
  downloadBusy: new Set(), eventSignature: '', resultSignature: '', notifiedRun: null,
};
let cooldownTimer;
let toastTimer;
const num = value => typeof value === 'number' && Number.isFinite(value) ? numberFormat.format(value) : '—';
const decimal = value => typeof value === 'number' && Number.isFinite(value) ? decimalFormat.format(value) : '—';
const pct = value => `${decimal(value)}%`;
const signed = value => `${value > 0 ? '+' : ''}${num(value)}`;
const activeRun = () => ['queued', 'running'].includes(state.run?.status);
const cooling = () => Date.now() < state.cooldownUntil;
const safeText = value => ['string', 'number', 'boolean'].includes(typeof value) ? String(value) : '—';
const node = (tag, className, text) => {
  const el = document.createElement(tag);
  if (className) el.className = className;
  if (text !== undefined) el.textContent = text;
  return el;
};
function icon(name) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('class', 'icon small');
  svg.setAttribute('aria-hidden', 'true');
  const use = document.createElementNS(svg.namespaceURI, 'use');
  use.setAttribute('href', `#i-${name}`);
  svg.append(use);
  return svg;
}
function notice(title, message, kind = 'error', action) {
  $('notice').className = `notice ${kind}`;
  $('notice-title').textContent = title;
  $('notice-text').textContent = message;
  $('notice').hidden = false;
  $('notice-action').hidden = !action;
  $('notice-action').onclick = action || null;
  updateControls();
}
function formError(message) {
  $('form-error').textContent = message;
  $('form-error').hidden = !message;
  if (message) $('form-error').focus({ preventScroll: true });
}
function toast(message) {
  clearTimeout(toastTimer);
  $('toast').querySelector('span').textContent = message;
  $('toast').hidden = false;
  toastTimer = setTimeout(() => { $('toast').hidden = true; }, 4500);
}
function setConnection(connected, label) {
  state.connected = connected;
  $('connection-label').textContent = label || (connected ? 'Сервер подключён' : 'Нет подключения');
  $('connection-dot').className = `status-dot ${connected ? 'online' : 'offline'}`;
  updateControls();
}
function stopPoll() { clearTimeout(state.pollTimer); state.pollTimer = null; }
function handleError(error, title = 'Не удалось выполнить запрос') {
  stopPoll();
  state.pollPaused = true;
  const message = error instanceof ApiError ? error.message : 'Неожиданная ошибка. Обновите состояние страницы.';
  if (error.status === 401) {
    setConnection(false, 'Нужен доступ');
    notice('Нужен токен доступа', message, 'warning', () => $('connection-dialog').showModal());
    $('notice-action').textContent = 'Подключиться';
    if (!$('connection-dialog').open) $('connection-dialog').showModal();
  } else if (error.status === 429) {
    state.cooldownUntil = Date.now() + Math.max(1, error.retryAfter ?? 60) * 1000;
    notice('Слишком много запросов', `Сервер просит подождать ${Math.ceil((state.cooldownUntil - Date.now()) / 1000)} сек. Затем обновление продолжится.`, 'warning');
    clearTimeout(cooldownTimer);
    cooldownTimer = setTimeout(() => {
      updateControls();
      if (!document.hidden) void refresh();
    }, Math.max(1, state.cooldownUntil - Date.now()) + 100);
  } else {
    if (error.status === 0 || error.status === 403) setConnection(false);
    notice(title, message, 'error', () => void refresh());
    $('notice-action').textContent = 'Обновить';
  }
  updateControls();
}
function updateControls() {
  const locked = state.submitting || activeRun();
  const unavailable = !state.connected || state.refreshing || cooling() || state.uncertain;
  const runtimeMissing = !state.overview || (state.overview.runtime?.missing_files?.length ?? 0) > 0 || state.overview.agent?.ready === false;
  const inputReady = state.source === 'upload' ? roles.every(role => state.files[role]) : state.overview?.runtime?.ready;
  $('start-button').disabled = locked || unavailable || runtimeMissing || !inputReady;
  $('start-label').textContent = state.submitting ? 'Отправляем данные…' : activeRun() ? 'Агент работает…' : state.uncertain ? 'Сначала обновите статус' : cooling() ? 'Ожидаем сервер…' : 'Запустить агента';
  $('run-form').setAttribute('aria-busy', String(state.submitting));
  $('refresh-button').disabled = state.refreshing || state.submitting || cooling();
  $('refresh-button').classList.toggle('is-loading', state.refreshing);
  $('notice-action').disabled = state.refreshing || cooling();
  $('connection-button').disabled = state.submitting;
  $('connect-submit').disabled = state.refreshing || cooling() || state.submitting;
  $('forget-token').disabled = state.refreshing || state.submitting;
  $('seed').disabled = locked;
  for (const role of roles) {
    $(`file-${role}`).disabled = locked;
    document.querySelector(`[data-remove="${role}"]`).disabled = locked;
  }
  $('clear-files').disabled = locked;
  document.querySelectorAll('[data-source]').forEach(el => { el.disabled = locked; });
  $('download-csv').disabled = state.run?.status !== 'completed' || !state.connected || cooling() || state.downloadBusy.has('csv');
  $('download-json').disabled = !state.run || !state.connected || cooling() || state.downloadBusy.has('json');
  document.querySelectorAll('[data-file-id]').forEach(el => { el.disabled = !state.connected || cooling() || state.downloadBusy.has(`data:${el.dataset.fileId}`); });
}
function renderOverview() {
  const overview = state.overview;
  if (!overview) return;
  $('limit-budget').textContent = num(overview.limits.total_budget);
  $('limit-contacts').textContent = num(overview.limits.max_total_contacts);
  $('limit-pilots').textContent = num(overview.limits.max_pilots);
  $('pilot-progress').max = overview.limits.max_pilots || 1;
  $('agent-engine').textContent = overview.agent.llm_calls ? `OpenAI + Python · ${overview.agent.model || 'модель сервера'}` : 'Python · без сетевых LLM-вызовов';
  $('privacy-copy').textContent = overview.agent.llm_calls ? 'CSV получает BeeSmart. В OpenAI передаются обезличенные агрегаты.' : 'Файлы отправляются вашему серверу BeeSmart';
  const dataset = overview.dataset;
  const ready = dataset.status === 'ready';
  $('bundled-title').textContent = ready ? 'Данные на сервере готовы' : 'На сервере пока нет готового набора';
  $('bundled-description').textContent = ready ? `${num(dataset.customers)} абонентов · ${num(dataset.tariffs)} тарифов` : dataset.message;
  $('bundled-files').replaceChildren();
  for (const file of overview.files) {
    const button = node('button', 'button secondary compact', file.name);
    button.type = 'button';
    button.dataset.fileId = file.id;
    button.prepend(icon('download'));
    button.addEventListener('click', () => void download(`data:${file.id}`, () => api.downloadData(file.id)));
    $('bundled-files').append(button);
  }
  if (overview.runtime.missing_files.length) {
    notice('Сервер не готов к расчёту', `Не хватает файлов среды: ${overview.runtime.missing_files.join(', ')}. Сохранённые результаты доступны для просмотра.`, 'warning');
  } else if (overview.agent.ready === false) {
    notice('Агент не готов к запуску', overview.agent.status === 'storage_error' ? 'Сервер сообщает об ошибке журнала расходов. Администратору нужно проверить хранилище BeeSmart.' : 'Настройте ключ OpenAI на сервере или выберите локальный режим в настройках backend. Ключ OpenAI не нужно вводить в браузер.', 'warning');
  }
  updateControls();
}
async function refresh() {
  if (state.refreshing || state.submitting || cooling()) return false;
  state.cooldownUntil = 0;
  stopPoll();
  const revision = ++state.revision;
  state.refreshing = true;
  updateControls();
  try {
    // Read the protected overview first: health alone does not establish access.
    const overview = await api.overview();
    if (revision !== state.revision) return false;
    state.overview = overview;
    setConnection(true);
    $('notice').hidden = true;
    renderOverview();
    let run;
    try { run = await api.latestRun(); }
    catch (error) { if (error.status === 404) run = null; else throw error; }
    if (revision !== state.revision) return false;
    state.uncertain = false;
    state.pollPaused = false;
    acceptRun(run);
    return true;
  } catch (error) {
    if (revision === state.revision) handleError(error, 'Не удалось обновить состояние');
    return false;
  } finally {
    if (revision === state.revision) {
      state.refreshing = false;
      updateControls();
      schedulePoll();
    }
  }
}
function schedulePoll() {
  stopPoll();
  if (!activeRun() || state.pollPaused || document.hidden || !state.connected || cooling() || state.pollBusy || state.refreshing) return;
  state.pollTimer = setTimeout(() => void poll(), 1200);
}
async function poll() {
  if (!activeRun() || state.pollBusy || state.refreshing || state.pollPaused || document.hidden || cooling()) return;
  const id = state.run.id;
  const revision = state.revision;
  state.pollBusy = true;
  try {
    const run = await api.run(id);
    if (revision === state.revision && state.run?.id === id) acceptRun(run);
  } catch (error) {
    if (revision === state.revision) handleError(error, 'Обновление расчёта приостановлено');
  } finally {
    state.pollBusy = false;
    schedulePoll();
  }
}
function acceptRun(run) {
  const previousStatus = state.run?.status;
  const previousId = state.run?.id;
  state.run = run;
  if (previousId !== run?.id) {
    state.eventSignature = '';
    state.resultSignature = '';
  }
  renderAgent();
  renderResults();
  renderEvents();
  updateControls();
  if (run?.status === 'failed') {
    notice('Расчёт остановлен', run.error || 'Сервер не смог завершить расчёт. Проверьте отчёт и входные данные.');
  } else if (run?.status === 'completed' && run.error) {
    notice('Расчёт завершён с предупреждением', run.error, 'warning');
  }
  if (run?.status === 'completed' && previousId === run.id && ['queued', 'running'].includes(previousStatus) && state.notifiedRun !== run.id) {
    state.notifiedRun = run.id;
    toast(run.metrics?.net_arpu_gain > 0 ? 'Расчёт готов. План кампаний можно скачать.' : 'Расчёт готов. Проверьте экономический результат.');
  }
  schedulePoll();
}
function renderAgent() {
  const run = state.run;
  const events = run?.events || [];
  const pilots = run?.metrics?.n_pilots ?? events.filter(event => event.event === 'pilot_result').length;
  const maxPilots = state.overview?.limits.max_pilots;
  $('pilot-counter').textContent = maxPilots !== undefined ? `${pilots} / ${maxPilots}` : '—';
  $('pilot-progress').value = pilots;
  $('run-seed-label').textContent = run ? `Seed ${run.seed}` : '';
  for (const id of ['stage-data', 'stage-pilots', 'stage-plan']) $(id).className = '';
  let label = 'ОЖИДАЕТ ДАННЫЕ';
  let title = 'Готов к следующему шагу';
  let description = 'Выберите аудиторию — остальное агент проверит на пилотах.';
  let dot = state.connected ? 'online' : 'offline';
  if (!run && state.overview?.agent?.ready === false) {
    label = 'ТРЕБУЕТСЯ НАСТРОЙКА'; title = 'Агент пока не готов';
    description = 'Проверьте настройки агента на сервере. Подробности указаны в сообщении выше.';
    dot = 'offline';
  }
  if (state.submitting) {
    label = 'ПОДГОТОВКА'; title = 'Проверяем ваши файлы';
    description = 'Сервер проверяет формат, колонки и согласованность данных.';
    $('stage-data').className = 'is-active'; dot = 'busy';
  } else if (run) {
    if (run.status === 'queued') {
      label = 'В ОЧЕРЕДИ'; title = 'Агент готовится к работе';
      description = 'Данные приняты. Сервер запускает расчёт.';
      $('stage-data').className = 'is-active'; dot = 'busy';
    } else if (run.status === 'running') {
      label = 'РАСЧЁТ В ПРОЦЕССЕ'; title = pilots ? 'Проверяем гипотезы' : 'Изучаем аудиторию';
      description = pilots ? 'Агент сравнивает реакцию групп и уточняет будущий план.' : 'Формируем группы абонентов и очередь тарифных гипотез.';
      const dataReady = events.some(event => event.event === 'run_start');
      $('stage-data').className = dataReady ? 'is-complete' : 'is-active';
      if (dataReady) $('stage-pilots').className = 'is-active';
      if (run.llm?.provider === 'openai' && ['pending', 'planning'].includes(run.llm.status)) {
        title = 'ИИ выбирает гипотезы';
        description = 'Модель анализирует агрегаты и предлагает порядок пилотов. Результат затем проверит Python-агент.';
      }
      if (events.some(event => event.event === 'run_end')) {
        $('stage-pilots').className = 'is-complete'; $('stage-plan').className = 'is-active';
        title = 'Оцениваем итоговый план'; description = 'Считаем результат с учётом всех контактов и пересечений.';
      }
      dot = 'busy';
    } else if (run.status === 'completed') {
      label = 'РАСЧЁТ ЗАВЕРШЁН'; title = 'Ваш план готов';
      description = run.metrics?.net_arpu_gain > 0 ? 'Аудитории и каналы выбраны. Изучите результат и скачайте кампании.' : 'Симуляция завершена без положительного чистого прироста. Изучите результат перед использованием плана.';
      for (const id of ['stage-data', 'stage-pilots', 'stage-plan']) $(id).className = 'is-complete';
    } else {
      label = 'ОШИБКА РАСЧЁТА'; title = 'Не удалось завершить';
      description = run.error || 'Проверьте данные и состояние сервера.';
      $(pilots ? 'stage-pilots' : 'stage-data').className = 'is-failed'; dot = 'offline';
    }
  }
  $('agent-state-label').textContent = label;
  $('agent-state-title').textContent = title;
  $('agent-state-description').textContent = description;
  $('agent-status-dot').className = `status-dot ${dot}`;
  $('pilot-stage-description').textContent = pilots ? `Получено результатов: ${pilots}` : 'Пилоты на небольших группах';
  renderLLM();
  renderTime();
}
function renderLLM() {
  const llm = state.run?.llm;
  $('llm-details').hidden = llm?.provider !== 'openai';
  if (llm?.provider !== 'openai') return;
  const labels = { pending: 'Подготовка ИИ-планирования', planning: 'Модель составляет очередь пилотов', cached: 'План ИИ из кэша', completed: 'Обоснование гипотез от ИИ', failed: 'Этап ИИ не завершён' };
  $('llm-title').textContent = labels[llm.status] || 'Планирование ИИ';
  // The model rationale is untrusted prose, never HTML or a measured result.
  $('llm-summary').textContent = typeof llm.summary === 'string' && llm.summary ? llm.summary : llm.status === 'failed' ? 'Причина указана в сообщении об ошибке расчёта.' : 'Ожидаем подготовку гипотез.';
  const parts = [];
  if (['completed', 'cached'].includes(llm.status)) parts.push(`${num(llm.hypotheses)} гипотез`, 'Обоснование, не измеренный эффект');
  if (llm.cache_hit) parts.push('Без нового запроса');
  if (typeof llm.estimated_cost_usd === 'number') parts.push(`Оценка расхода: ${usdFormat.format(llm.estimated_cost_usd)} USD`);
  if (llm.reserved_usd > 0) parts.push(`Резерв: ${usdFormat.format(llm.reserved_usd)} USD`);
  $('llm-usage').textContent = parts.join(' · ');
}
function renderTime() {
  const run = state.run;
  if (!run) { $('run-time').textContent = state.submitting ? 'Загрузка файлов…' : 'Расчёт ещё не запущен'; return; }
  let seconds = run.duration_seconds;
  if (seconds == null && activeRun()) seconds = Math.max(0, (Date.now() - Date.parse(run.created_at)) / 1000);
  const duration = Number.isFinite(seconds) ? seconds < 60 ? `${decimal(seconds)} сек.` : `${Math.floor(seconds / 60)} мин. ${Math.floor(seconds % 60)} сек.` : '—';
  $('run-time').textContent = `${activeRun() ? 'Прошло' : 'Длительность'}: ${duration}`;
}
function campaignRows() {
  const run = state.run;
  const details = run?.metrics?.campaigns_detail?.slice(run.metrics.n_pilots) || [];
  return (run?.campaigns || []).map((campaign, index) => ({ campaign, detail: details[index]?.name === campaign.campaign_name ? details[index] : null }));
}
function renderResults(force = false) {
  const run = state.run;
  const signature = `${run?.id}:${run?.status}:${run?.metrics?.n_final_campaigns}:${run?.error}`;
  if (!force && signature === state.resultSignature) return;
  state.resultSignature = signature;
  const metrics = run?.status === 'completed' ? run.metrics : null;
  const statuses = { queued: 'В очереди', running: 'Агент работает', completed: metrics?.net_arpu_gain > 0 ? 'Готово · прирост в плюс' : 'Готово · без прибыли', failed: 'Ошибка расчёта' };
  $('result-status').textContent = statuses[run?.status] || 'Ожидает расчёта';
  $('result-status').className = `result-status ${run?.status === 'completed' ? metrics?.net_arpu_gain > 0 ? 'positive' : 'negative' : activeRun() ? 'is-active' : ''}`;
  $('metric-net').textContent = metrics ? signed(metrics.net_arpu_gain) : '—';
  $('metric-net').className = metrics ? metrics.net_arpu_gain > 0 ? 'positive' : 'negative' : '';
  $('metric-net-note').textContent = metrics ? `у.е. · ${pct(metrics.growth_vs_baseline_pct)} к базовому ARPU` : 'Результат после всех расходов';
  $('metric-cost').textContent = metrics ? num(metrics.total_cost) : '—';
  $('metric-cost-note').textContent = metrics ? `у.е. · ${pct(metrics.budget_used_pct)} бюджета` : 'Пилоты и финальный план';
  $('metric-audience').textContent = metrics ? num(metrics.unique_customers_targeted) : '—';
  $('metric-audience-note').textContent = metrics ? `${pct(metrics.coverage_pct)} базы · ${num(metrics.total_contacts)} контактов` : 'Каждый абонент учтён один раз';
  $('metric-campaigns').textContent = metrics ? num(metrics.n_final_campaigns) : '—';
  $('metric-campaigns-note').textContent = metrics ? `Плюс ${num(metrics.n_pilots)} пилотов для проверки` : `До ${state.overview?.limits.max_campaigns ?? 10} кампаний в одном плане`;
  $('nav-campaign-count').textContent = metrics ? num(metrics.n_final_campaigns) : '—';
  $('table-count').textContent = String(run?.campaigns?.length || 0);
  const completed = run?.status === 'completed';
  const hasCampaigns = completed && run.campaigns.length > 0;
  $('results-content').hidden = !hasCampaigns;
  $('results-empty').hidden = hasCampaigns;
  if (completed) {
    const source = run.dataset.source === 'uploaded' ? 'Загруженные CSV' : 'Набор на сервере';
    const size = run.dataset.customers != null ? ` · ${num(run.dataset.customers)} абонентов` : '';
    $('result-description').textContent = `${source}${size} · Seed ${run.seed}`;
  } else $('result-description').textContent = 'Выбранные аудитории, тарифы и каналы связи';
  if (activeRun()) {
    $('empty-title').textContent = 'Агент работает над вашим планом';
    $('empty-description').textContent = 'Результаты появятся автоматически после завершения расчёта.';
  } else if (run?.status === 'failed') {
    $('empty-title').textContent = 'Расчёт не завершён';
    $('empty-description').textContent = 'Проверьте сообщение об ошибке. Подробности доступны в отчёте JSON.';
  } else if (completed && !hasCampaigns) {
    $('empty-title').textContent = 'Сервер вернул пустой план';
    $('empty-description').textContent = 'Подробности доступны в отчёте JSON.';
  } else {
    $('empty-title').textContent = 'Хороший план начинается с данных';
    $('empty-description').textContent = 'После пилотов здесь появятся кампании с выбранными аудиториями и тарифами.';
  }
  renderTable();
  renderChannels();
}
function renderTable() {
  const query = $('campaign-search').value.trim().toLocaleLowerCase('ru');
  const channel = $('channel-filter').value;
  const rows = campaignRows().filter(({ campaign: c }) => (channel === 'all' || c.channel === channel) && [c.campaign_name, c.target_tariff, c.filter_current_tariff, c.filter_arpu_segment].filter(Boolean).join(' ').toLocaleLowerCase('ru').includes(query));
  const fragment = document.createDocumentFragment();
  for (const { campaign: c, detail: d } of rows) {
    const row = node('tr');
    const audience = node('td');
    audience.append(node('strong', 'audience-title', c.filter_current_tariff || 'Все текущие тарифы'));
    const labels = [`ARPU: ${c.filter_arpu_segment || 'все'}`, `Интернет: ${c.filter_data_segment || 'все'}`, `Звонки: ${c.filter_call_segment || 'все'}`];
    audience.append(node('span', 'audience-meta', labels.join(' · ')));
    audience.title = c.campaign_name;
    const target = node('td'); target.append(node('span', 'tariff-name', c.target_tariff));
    const channelCell = node('td');
    const pill = node('span', `channel-pill channel-${c.channel}`, channelNames[c.channel] || c.channel);
    pill.prepend(icon(c.channel === 'call' ? 'phone' : 'message'));
    channelCell.append(pill);
    const contacts = node('td', 'table-number', num(d?.n_contacts));
    if (d && (d.capped_at_campaign_limit || d.capped_at_reach_budget || d.capped_at_money_budget)) {
      contacts.append(node('small', 'audience-meta', 'Ограничено лимитом'));
    }
    const cost = node('td', 'table-number', num(d?.cost));
    const gain = node('td', `table-number ${d?.gross_lift > 0 ? 'positive' : d?.gross_lift < 0 ? 'negative' : ''}`, d ? signed(d.gross_lift) : '—');
    row.append(audience, target, channelCell, contacts, cost, gain);
    fragment.append(row);
  }
  $('campaign-rows').replaceChildren(fragment);
  $('no-matching-campaigns').hidden = rows.length !== 0;
}
function renderChannels() {
  const counts = {};
  for (const { campaign, detail } of campaignRows()) if (detail) counts[campaign.channel] = (counts[campaign.channel] || 0) + detail.n_contacts;
  const total = Object.values(counts).reduce((a, b) => a + b, 0);
  const legend = document.createDocumentFragment();
  const chart = $('channel-chart'); chart.replaceChildren();
  let offset = 0;
  for (const [channel, count] of Object.entries(counts)) {
    const item = node('span', `channel-legend-item channel-${channel}`);
    item.append(node('i', 'legend-dot'), node('span', '', `${channelNames[channel] || channel} ${num(count)}`));
    legend.append(item);
    if (!total) continue;
    const rect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
    const width = count / total * 400;
    for (const [key, value] of Object.entries({ x: offset, y: 0, width, height: 16, fill: channelColors[channel] || '#aaa' })) rect.setAttribute(key, String(value));
    chart.append(rect);
    offset += width;
  }
  if (!Object.keys(counts).length) legend.append(node('span', '', 'Нет данных о контактах'));
  $('channel-legend').replaceChildren(legend);
  chart.setAttribute('aria-label', `Контакты финального плана: ${Object.entries(counts).map(([channel, count]) => `${channelNames[channel]} ${num(count)}`).join(', ') || 'нет данных'}`);
}
function describeEvent(event) {
  const d = event.data || {};
  switch (event.event) {
    case 'agent_planning': return { title: 'ИИ готовит гипотезы', description: `Модель ${safeText(d.model)} анализирует агрегаты аудитории.`, meta: 'Измеренные эффекты будут получены на пилотах', icon: 'spark' };
    case 'agent_policy_ready': return { title: 'Очередь гипотез готова', description: `${num(d.hypotheses)} гипотез переданы Python-агенту`, meta: d.cache_hit ? 'Использована сохранённая политика' : 'Модель подготовила новую политику', icon: 'check' };
    case 'pilot_result': return { title: `Пилот ${safeText(d.pilot_number)} · ${channelNames[d.channel] || safeText(d.channel)}`, description: `${safeText(d.current_tariff)} / ${safeText(d.arpu_segment)} → ${safeText(d.target_tariff)}`, meta: `${num(d.n_customers)} абонентов · ${num(d.cost)} у.е. · наблюдение ${pct(typeof d.observed_lift_ratio === 'number' ? d.observed_lift_ratio * 100 : null)} · нижняя оценка ${pct(typeof d.lower === 'number' ? d.lower * 100 : null)}`, icon: 'target' };
    case 'run_start': return { title: 'Аудитория изучена', description: `${num(d.profile_size)} абонентов · ${num(d.queue_size)} гипотез в очереди`, meta: `Бюджет ${num(d.budget)} у.е. · ${num(d.contacts)} контактов`, icon: 'users' };
    case 'arm_selected': return { title: 'Выбрана гипотеза', description: Array.isArray(d.arm) ? d.arm.map(safeText).join(' → ') : 'Гипотеза передана на проверку', meta: `${num(d.requested_n)} контактов · ${channelNames[d.channel] || safeText(d.channel)} · ${d.reason === 'confirmation' ? 'уточнение оценки' : 'первая проверка'}`, icon: 'spark' };
    case 'plan_selected': return { title: 'План пересчитан', description: `${num(Array.isArray(d.campaigns) ? d.campaigns.length : null)} кампаний · ${num(d.contacts)} контактов`, meta: `${num(d.cost)} у.е. · ${d.fallback ? 'резервный план' : 'план по подтверждённым гипотезам'}`, icon: 'layers' };
    case 'explore_decision': return { title: d.action === 'stop' ? 'Разведка завершена' : 'Уточняем гипотезу', description: d.reason === 'no_profitable_confirmation' ? 'Дополнительные пилоты не улучшают консервативную оценку плана.' : 'Агент проверил ценность следующего пилота.', meta: '', icon: 'activity' };
    case 'pilot_error': return { title: 'Пилот не завершён', description: safeText(d.error_type), meta: 'Автоматический повтор не выполняется', icon: 'info' };
    case 'run_end': return { title: 'Агент сформировал план', description: 'Кампании переданы на итоговую оценку.', meta: `Остаток: ${num(d.remaining_budget)} у.е. · ${num(d.remaining_contacts)} контактов`, icon: 'check' };
    default: return { title: event.event, description: 'Событие сервера. Полные данные доступны в отчёте JSON.', meta: '', icon: 'activity' };
  }
}
function renderEvents(force = false) {
  const all = state.run?.events || [];
  const filter = $('event-filter').value;
  const signature = `${state.run?.id}:${all.length}:${filter}`;
  if (!force && signature === state.eventSignature) return;
  state.eventSignature = signature;
  const filtered = filter === 'pilots' ? all.filter(event => ['pilot_result', 'pilot_error'].includes(event.event)) : all;
  const visible = filtered.slice(-160).reverse();
  $('event-count').textContent = num(filtered.length);
  $('event-list').hidden = !visible.length;
  $('activity-empty').hidden = visible.length > 0;
  $('activity-empty').querySelector('p').textContent = all.length && filter === 'pilots' ? 'Результатов пилотов пока нет. Другие события доступны через фильтр.' : 'Здесь будет виден ход проверки гипотез.';
  const fragment = document.createDocumentFragment();
  if (filtered.length > 160) fragment.append(node('li', 'event-description', 'Показаны последние 160 событий. Полный журнал — в JSON.'));
  for (const event of visible) {
    const description = describeEvent(event);
    const item = node('li', 'event-item');
    const badge = node('span', 'event-icon'); badge.append(icon(description.icon));
    const body = node('div', 'event-body');
    body.append(node('strong', 'event-title', description.title), node('p', 'event-description', description.description));
    if (description.meta) body.append(node('span', 'event-meta', description.meta));
    item.append(badge, body);
    fragment.append(item);
  }
  $('event-list').replaceChildren(fragment);
}
function selectSource(source) {
  if (state.submitting || activeRun()) return;
  state.source = source;
  for (const tab of document.querySelectorAll('[data-source]')) {
    const selected = tab.dataset.source === source;
    tab.setAttribute('aria-selected', String(selected)); tab.tabIndex = selected ? 0 : -1;
    tab.classList.toggle('active', selected);
  }
  $('upload-source').hidden = source !== 'upload';
  $('bundled-source').hidden = source !== 'bundled';
  formError(''); updateControls();
}
function setFile(role, file) {
  if (state.submitting || activeRun()) return;
  if (file && (!/\.csv$/i.test(file.name) || file.size === 0 || file.size > fileLimits[role])) {
    formError(!/\.csv$/i.test(file.name) ? 'Выберите файл с расширением .csv.' : file.size === 0 ? 'Файл пуст. Выберите CSV с данными.' : `Файл «${file.name}» слишком большой. ${limitLabels[role]}.`);
    $(`file-${role}`).value = '';
    return;
  }
  if (file) state.files[role] = file; else { delete state.files[role]; $(`file-${role}`).value = ''; }
  formError('');
  renderFiles();
}
function renderFiles() {
  for (const role of roles) {
    const card = document.querySelector(`[data-role="${role}"]`);
    const file = state.files[role];
    card.classList.toggle('is-selected', Boolean(file));
    card.querySelector('[data-file-name]').textContent = file?.name || 'Выбрать CSV';
    card.querySelector('[data-file-name]').title = file?.name || '';
    card.querySelector('[data-file-size]').textContent = file ? file.size >= 1024 ** 2 ? `${decimal(file.size / 1024 ** 2)} МиБ · готов к загрузке` : `${decimal(file.size / 1024)} КиБ · готов к загрузке` : limitLabels[role];
    card.querySelector('.file-choice use').setAttribute('href', file ? '#i-check' : '#i-upload');
    card.querySelector('[data-remove]').hidden = !file;
  }
  const count = roles.filter(role => state.files[role]).length;
  $('file-selection-label').textContent = count ? `Выбрано ${count} из 3 файлов` : 'Можно перетащить файл в нужную область';
  $('clear-files').hidden = !count;
  updateControls();
}
async function startRun(event) {
  event.preventDefault();
  if ($('start-button').disabled) return;
  const value = $('seed').value.trim();
  const seed = Number(value);
  if (!value || !Number.isInteger(seed) || seed < 0 || seed > 4_294_967_295) {
    formError('Seed должен быть целым числом от 0 до 4294967295.'); $('seed').focus(); return;
  }
  state.submitting = true;
  state.revision += 1;
  stopPoll(); formError(''); $('notice').hidden = true;
  updateControls(); renderAgent();
  try {
    const run = state.source === 'upload' ? await api.uploadAndRun({ ...state.files, seed }) : await api.startRun(seed);
    state.pollPaused = false;
    state.submitting = false;
    $('campaign-search').value = ''; $('channel-filter').value = 'all';
    acceptRun(run);
    toast('Данные приняты. Агент приступил к расчёту.');
  } catch (error) {
    if ([400, 413, 422].includes(error.status)) formError(error.message);
    else if (error.status === 409) {
      state.submitting = false;
      await refresh();
      if (activeRun()) toast('На сервере уже идёт расчёт. Показываем его состояние.');
      else notice('Сервер занят', error.message, 'warning', () => void refresh());
    } else {
      // A timeout/5xx may occur after the server accepted the POST. Never resend automatically.
      if (error.status === 0 || error.status >= 500) state.uncertain = true;
      handleError(error, 'Не удалось подтвердить запуск');
    }
  } finally {
    state.submitting = false;
    updateControls(); renderAgent(); schedulePoll();
  }
}
async function download(key, request) {
  if (state.downloadBusy.has(key) || cooling() || !state.connected) return;
  const revision = state.revision;
  state.downloadBusy.add(key); updateControls();
  try {
    const { blob, filename } = await request();
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url; link.download = filename;
    document.body.append(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 60_000);
    toast(`Файл ${filename} готов`);
  } catch (error) {
    // A response using a previous connection must not invalidate a new login.
    if (revision === state.revision) handleError(error, 'Не удалось скачать файл');
  }
  finally { state.downloadBusy.delete(key); updateControls(); }
}

$('server-address').textContent = location.origin;
$('run-form').addEventListener('submit', startRun);
$('refresh-button').addEventListener('click', () => void refresh());
$('notice-close').addEventListener('click', () => { $('notice').hidden = true; });
$('connection-button').addEventListener('click', () => { $('connection-error').hidden = true; $('connection-dialog').showModal(); });
for (const id of ['open-guide', 'format-button', 'footer-help']) $(id).addEventListener('click', () => $('guide-dialog').showModal());
document.querySelectorAll('[data-close-dialog]').forEach(button => button.addEventListener('click', () => button.closest('dialog').close()));
$('connection-dialog').addEventListener('close', () => { $('api-token').value = ''; });
$('connection-form').addEventListener('submit', async event => {
  event.preventDefault();
  if (state.refreshing || state.submitting || cooling()) return;
  api.setToken($('api-token').value);
  $('api-token').value = '';
  $('connection-error').hidden = true;
  if (await refresh()) { $('connection-dialog').close(); toast('Подключение установлено'); }
  else { $('connection-error').textContent = $('notice-text').textContent; $('connection-error').hidden = false; }
});
$('forget-token').addEventListener('click', async () => {
  api.setToken(''); $('api-token').value = '';
  state.revision += 1; setConnection(false); stopPoll();
  if (await refresh()) { $('connection-dialog').close(); toast('Токен удалён из памяти'); }
});
document.querySelectorAll('[data-source]').forEach(tab => {
  tab.addEventListener('click', () => selectSource(tab.dataset.source));
  tab.addEventListener('keydown', event => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    const source = event.key === 'Home' ? 'upload' : event.key === 'End' ? 'bundled' : state.source === 'upload' ? 'bundled' : 'upload';
    selectSource(source); $(`tab-${source}`).focus();
  });
});
for (const role of roles) {
  $(`file-${role}`).addEventListener('change', event => { if (event.target.files[0]) setFile(role, event.target.files[0]); });
  document.querySelector(`[data-remove="${role}"]`).addEventListener('click', () => setFile(role, null));
  const card = document.querySelector(`[data-role="${role}"]`);
  card.addEventListener('dragover', event => { event.preventDefault(); if (!state.submitting && !activeRun()) card.classList.add('is-dragging'); });
  card.addEventListener('dragleave', event => { if (!card.contains(event.relatedTarget)) card.classList.remove('is-dragging'); });
  card.addEventListener('drop', event => {
    event.preventDefault(); card.classList.remove('is-dragging');
    if (state.submitting || activeRun()) return;
    if (event.dataTransfer.files.length !== 1) { formError('Перетащите один CSV в соответствующую область.'); return; }
    setFile(role, event.dataTransfer.files[0]);
  });
}
// Prevent accidental browser navigation when a file is dropped outside a card.
window.addEventListener('dragover', event => { if (event.dataTransfer.types.includes('Files')) event.preventDefault(); });
window.addEventListener('drop', event => { if (event.dataTransfer.types.includes('Files')) event.preventDefault(); });
$('clear-files').addEventListener('click', () => { for (const role of roles) setFile(role, null); });
$('campaign-search').addEventListener('input', renderTable);
$('channel-filter').addEventListener('change', renderTable);
$('event-filter').addEventListener('change', () => renderEvents(true));
$('download-csv').addEventListener('click', () => { if (state.run) void download('csv', () => api.downloadRun(state.run.id, 'csv')); });
$('download-json').addEventListener('click', () => { if (state.run) void download('json', () => api.downloadRun(state.run.id, 'json')); });
document.querySelectorAll('.nav-item').forEach(link => link.addEventListener('click', () => {
  document.querySelectorAll('.nav-item').forEach(item => { item.classList.toggle('active', item === link); if (item === link) item.setAttribute('aria-current', 'location'); else item.removeAttribute('aria-current'); });
}));
document.addEventListener('visibilitychange', () => {
  if (document.hidden) stopPoll();
  else if (state.cooldownUntil && !cooling()) void refresh();
  else schedulePoll();
});
window.addEventListener('online', () => { if (!state.connected && !state.refreshing && !state.submitting) void refresh(); });
window.addEventListener('offline', () => { setConnection(false); stopPoll(); notice('Соединение прервано', 'Расчёт может продолжаться на сервере. Состояние обновится после восстановления связи.', 'warning'); });
setInterval(() => { if (!document.hidden && activeRun()) renderTime(); }, 1000);
void refresh();
