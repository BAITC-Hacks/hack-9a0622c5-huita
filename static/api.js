/** An API failure safe to display as text. retryAfter is seconds, or null. */
export class ApiError extends Error {
  constructor(status, detail, retryAfter = null) {
    super(detail);
    this.name = 'ApiError';
    this.status = status;
    this.detail = detail;
    this.retryAfter = retryAfter;
  }
}

const REQUEST_TIMEOUT = 30_000;
const UPLOAD_TIMEOUT = 60_000;

function retryDelay(header) {
  if (!header) return null;
  const seconds = Number(header);
  if (Number.isFinite(seconds) && seconds >= 0) return Math.ceil(seconds);
  const date = Date.parse(header);
  return Number.isFinite(date) ? Math.max(0, Math.ceil((date - Date.now()) / 1000)) : null;
}

function detailText(detail) {
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) {
    // Validation errors can contain the submitted input; show messages only.
    return detail.map(item => typeof item === 'string' ? item : item?.msg)
      .filter(item => typeof item === 'string').join('; ');
  }
  return '';
}

function safeFilename(disposition, fallback) {
  let filename = '';
  const encoded = /(?:^|;)\s*filename\*\s*=\s*(?:"([^"]*)"|([^;]*))/i.exec(disposition || '');
  if (encoded) {
    const value = (encoded[1] ?? encoded[2] ?? '').trim();
    const utf8 = /^UTF-8'[^']*'(.*)$/i.exec(value);
    if (utf8) {
      try { filename = decodeURIComponent(utf8[1]); } catch { /* Use the plain name. */ }
    }
  }
  if (!filename) {
    const plain = /(?:^|;)\s*filename\s*=\s*(?:"([^"]*)"|([^;]*))/i.exec(disposition || '');
    filename = plain ? (plain[1] ?? plain[2] ?? '').trim() : '';
  }
  // Do not let server-supplied names create paths or hide the real extension.
  filename = filename.split(/[\\/]/).pop()
    .replace(/[\u0000-\u001f\u007f<>:"|?*\u202a-\u202e\u2066-\u2069]/g, '')
    .replace(/^[.\s]+|[.\s]+$/g, '');
  const extension = fallback.slice(fallback.lastIndexOf('.'));
  if (!filename || filename.length > 180 || !filename.toLowerCase().endsWith(extension)) return fallback;
  return filename;
}

/** Calls only the configured BeeSmart backend. The token lives in memory. */
export class BeeSmartApi {
  #token = '';
  #baseUrl;
  #fetch;
  #readRequests = new Map();

  constructor({ token = '', baseUrl = '', fetchImpl = globalThis.fetch } = {}) {
    if (typeof fetchImpl !== 'function') throw new ApiError(0, 'Браузер не поддерживает сетевые запросы.');
    if (typeof baseUrl !== 'string') throw new ApiError(0, 'Некорректный адрес API.');
    this.#baseUrl = baseUrl.trim().replace(/\/+$/, '');
    // Native Window.fetch rejects a class instance as its receiver.
    this.#fetch = fetchImpl.bind(globalThis);
    this.setToken(token);
  }

  setToken(token) {
    const next = typeof token === 'string' ? token.trim() : '';
    if (next !== this.#token) this.cancelReadRequests({ includeDownloads: true });
    this.#token = next;
  }

  cancelReadRequests({ includeDownloads = false } = {}) {
    for (const [controller, isDownload] of this.#readRequests) {
      if (includeDownloads || !isDownload) controller.abort();
    }
  }

  health() { return this.#request('/api/health'); }
  overview() { return this.#request('/api/overview'); }
  agentInfo() { return this.#request('/api/agent'); }
  latestRun() { return this.#request('/api/runs/latest'); }

  async run(id) {
    return this.#request(`/api/runs/${this.#segment(id)}`);
  }

  async startRun(seed = 42) {
    this.#validateSeed(seed);
    return this.#request('/api/runs', { method: 'POST', json: { seed } });
  }

  async uploadAndRun({ profile, history, tariffs, seed = 42 } = {}) {
    this.#validateSeed(seed);
    const form = new FormData();
    for (const [name, file] of Object.entries({ profile, history, tariffs })) {
      if (!(file instanceof Blob)) throw new ApiError(422, 'Выберите три CSV: профиль, историю и тарифы.');
      form.append(name, file);
    }
    form.append('seed', String(seed));
    return this.#request('/api/uploads/run', { method: 'POST', body: form, timeout: UPLOAD_TIMEOUT });
  }

  async downloadRun(id, kind) {
    if (kind !== 'csv' && kind !== 'json') throw new ApiError(422, 'Выберите формат CSV или JSON.');
    const endpoint = kind === 'csv' ? 'submission.csv' : 'report.json';
    const fallback = kind === 'csv' ? 'submission.csv' : 'beesmart-report.json';
    return this.#request(`/api/runs/${this.#segment(id)}/${endpoint}`, { download: fallback });
  }

  async downloadData(fileId) {
    return this.#request(`/api/data/${this.#segment(fileId)}`, { download: 'beesmart-data.csv' });
  }

  #segment(value) {
    if (typeof value !== 'string' || !value) throw new ApiError(422, 'Не указан идентификатор.');
    try { return encodeURIComponent(value); }
    catch { throw new ApiError(422, 'Некорректный идентификатор.'); }
  }

  #validateSeed(seed) {
    if (!Number.isInteger(seed) || seed < 0 || seed > 4_294_967_295) {
      throw new ApiError(422, 'Seed должен быть целым числом от 0 до 4294967295.');
    }
  }

  #cleanMessage(message, requestToken) {
    let text = String(message);
    for (const token of new Set([requestToken, this.#token])) {
      if (!token) continue;
      text = text.split(token).join('[скрыто]');
      try { text = text.split(encodeURIComponent(token)).join('[скрыто]'); } catch { /* Invalid Unicode. */ }
    }
    return text.replace(/Bearer\s+[^\s,;"<>]+/gi, 'Bearer [скрыто]')
      .replace(/[\u0000-\u001f\u007f]/g, ' ').trim().slice(0, 600);
  }

  async #request(path, { method = 'GET', json, body, download, timeout = REQUEST_TIMEOUT } = {}) {
    const controller = new AbortController();
    if (method === 'GET') this.#readRequests.set(controller, Boolean(download));
    const requestToken = this.#token;
    let timedOut = false;
    const timer = setTimeout(() => { timedOut = true; controller.abort(); }, timeout);
    let status = 0;
    let retryAfter = null;
    try {
      const headers = new Headers({ Accept: download ? '*/*' : 'application/json' });
      if (requestToken) headers.set('Authorization', `Bearer ${requestToken}`);
      if (method === 'POST') headers.set('X-BeeSmart-Request', '1');
      if (json !== undefined) {
        headers.set('Content-Type', 'application/json');
        body = JSON.stringify(json);
      }
      const response = await this.#fetch(`${this.#baseUrl}${path}`, {
        method, headers, body, signal: controller.signal,
        credentials: 'omit', cache: 'no-store', redirect: 'error',
      });
      if (controller.signal.aborted) throw new Error('Request aborted');
      status = response.status;
      retryAfter = retryDelay(response.headers.get('Retry-After'));
      if (!response.ok) {
        const raw = await response.text();
        if (controller.signal.aborted) throw new Error('Request aborted');
        let message = '';
        try { message = detailText(JSON.parse(raw)?.detail); }
        catch {
          // Plain proxy errors are useful; HTML error pages should not reach the UI.
          if (response.headers.get('Content-Type')?.toLowerCase().startsWith('text/plain')) message = raw;
        }
        message = this.#cleanMessage(message, requestToken) || `Ошибка сервера (${status}).`;
        throw new ApiError(status, message, retryAfter);
      }
      if (download) {
        const blob = await response.blob();
        if (controller.signal.aborted) throw new Error('Request aborted');
        return { blob, filename: safeFilename(response.headers.get('Content-Disposition'), download) };
      }
      const raw = await response.text();
      if (controller.signal.aborted) throw new Error('Request aborted');
      try { return JSON.parse(raw); }
      catch { throw new ApiError(status, 'Сервер вернул ответ в неожиданном формате.', retryAfter); }
    } catch (error) {
      if (controller.signal.aborted && !timedOut) {
        const cancelled = new ApiError(0, 'Запрос отменён.');
        cancelled.aborted = true;
        throw cancelled;
      }
      if (timedOut) {
        throw new ApiError(0, 'Сервер не ответил вовремя. Проверьте состояние запуска перед повторной отправкой.');
      }
      if (error instanceof ApiError) throw error;
      // Browser/network errors may contain URLs or headers. Do not expose them.
      throw new ApiError(0, 'Не удалось связаться с сервером. Проверьте подключение и адрес API.');
    } finally {
      clearTimeout(timer);
      this.#readRequests.delete(controller);
    }
  }
}
