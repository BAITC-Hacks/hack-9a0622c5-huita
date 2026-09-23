# API для фронтенда BeeSmart

Backend: Python 3.12+, FastAPI. Frontend: HTML, CSS, JavaScript без Node.js.
Запуск: `python -m beesmart`; адрес: http://127.0.0.1:8000.
HTML размещается в `static/index.html`, остальные файлы — в `static/`.
Локально все запросы можно отправлять на тот же origin. В production используются
HTTPS и `Authorization: Bearer <токен BeeSmart>`. Отдельный frontend-origin разрешается
через `BEESMART_ALLOWED_ORIGINS`; wildcard запрещён. Доступ внутри одной команды общий.

Машиночитаемый контракт: **[open.json](../open.json)**; живые копии на сервере:
`/openapi.json` и `/open.json`. Они доступны без авторизации и содержат только схему.
В `components/schemas` описаны `RunRecord`, `Campaign`, `Metrics`, `OverviewResponse`
и ошибки. Обновление: `python scripts/export_openapi.py`; CI проверяет соответствие.

| Метод и путь | Ответ |
|---|---|
| `GET /api/health` | `{ "status": "ok" }` |
| `GET /api/agent` | Движок агента, модель, использование LLM и тип оценки |
| `GET /api/overview` | Профиль данных, сегменты, тарифы, лимиты, доступные CSV |
| `POST /api/uploads/run` | Загрузить три CSV и автоматически начать расчёт; multipart, ответ 202 |
| `POST /api/runs` | Начать расчёт; JSON `{ "seed": 42 }`; ответ 202 |
| `GET /api/runs/latest` | Последний запуск; 404 если запусков нет |
| `GET /api/runs/{id}` | Состояние, события пилотов, кампании и результаты |
| `GET /api/runs/{id}/submission.csv` | Скачать финальные кампании готового запуска |
| `GET /api/runs/{id}/report.json` | Скачать отчёт |
| `GET /api/data/{file_id}` | Исходный CSV; разрешённые ссылки в `overview.files` |
| `GET /openapi.json` | Машиночитаемая схема API |

Для всех POST обязателен `X-BeeSmart-Request: 1`. `/api/runs` принимает JSON,
а `/api/uploads/run` — `multipart/form-data`; браузер сам выставляет его Content-Type.
Seed — целое число от 0 до 4294967295. При активном расчёте второй POST получает 409.
Статусы запуска: `queued`, `running`, `completed`, `failed`. Опрос состояния нужен
только при первых двух статусах, примерно раз в секунду. При скрытой вкладке опрос
можно остановить. `error` содержит сообщение для пользователя.

Поля запуска: `id`, `seed`, `status`, `dataset`, `created_at`, `finished_at`, `duration_seconds`,
`events`, `campaigns`, `metrics`, `diagnostics`, `error`. Событие: `{event, data}`.
Пилоты приходят как `event="pilot_result"`. Метрики появляются после официального
локального scorer: `net_arpu_gain`, `gross_arpu_lift`, `total_cost`, `total_contacts`,
`unique_customers_targeted`, `n_pilots`, `n_final_campaigns`, `roi`, `coverage_pct`,
`budget_used_pct`, `risk_score_pct`, `status` (PASS/FAIL), `campaigns_detail`.
`roi=null` означает, что отношение не определено из-за нулевых расходов.

В `campaigns_detail` сначала идут пилоты, затем финал. `gross_lift` отдельной строки
считается до общей дедупликации; складывать его для получения net нельзя.
`diagnostics.gain_low` — оценка приращения финального плана, а не результат scorer.

В overview: `project`, `limits`, `dataset`, `runtime`, `segments`, `tariffs`,
`providers`, `files`. `runtime.ready=false` означает, что отсутствуют необходимые
данные/модули. Исходники и синтетические данные — оригинальная выдача организаторов.
Эффекты mock отличаются от скрытого судейства; результат UI не является будущим баллом.

Лимиты OpenAI и Brev/NVIDIA — по $50, раздельно. `app_spend_usd=0` относится к этому
приложению; баланс аккаунта не считывается (`account_balance_usd=null`). Сетевых
вызовов LLM во время `Agent.act` нет. API-ключи не передаются браузеру или worker.
`GET /api/agent` явно возвращает `engine="local_python"`, `model=null`,
`llm_calls=false`, `paid_calls=false`, `evaluation="organizer_mock"`.

Production-токен BeeSmart вводит пользователь; храните его только в памяти страницы.
Не вшивайте токены в HTML/JS, Git, URL или localStorage. Ключи OpenAI/Brev фронтенду
вообще не нужны. Для GET результатов и скачиваний также нужен заголовок Authorization.
Для скачивания используйте `fetch` с этим заголовком, затем `response.blob()` и
`URL.createObjectURL`; после скачивания вызывайте `URL.revokeObjectURL`.

## Сценарий: загрузить → рассчитать → показать результат

Пользователь выбирает три CSV, затем фронтенд одним запросом отправляет файлы:

| Поле multipart | Файл | Лимит |
|---|---|---|
| `profile` | `customer_profile.csv` | 32 МиБ, 100 000 строк |
| `history` | `data/change_tariff.csv` | 16 МиБ, 200 000 строк |
| `tariffs` | `data/dict_tariff.csv` | 256 КиБ, 100 тарифов |
| `seed` | Необязательное число, по умолчанию 42 | 0…4294967295 |

Общий размер запроса — до 50 МиБ, передача — до 30 секунд. CSV — UTF-8,
разделитель запятая, до 128 колонок; схема соответствует исходным файлам организаторов.
В чистом клоне CSV отсутствуют. Получите их из локального пакета организаторов.
Если исходники установлены в приватном `BEESMART_DATA_DIR`, авторизованный клиент
может скачать их по ссылкам из `GET /api/overview`, поля `files`.
Пропуски категорий из исходной выдачи допустимы; колонку `predicted_arpu` нужно
подготовить заранее. Сырые таблицы traffic/arpu_monthly этот маршрут не обрабатывает.
Нужны ячейка для пилота минимум из 10 абонентов и хотя бы один допустимый
финальный сегмент до 5 000 абонентов.

```javascript
// profileFile, historyFile, tariffsFile — File из трёх <input type="file">.
async function uploadAndRun(profileFile, historyFile, tariffsFile, onProgress,
                            { apiBase = '', accessToken = '' } = {}) {
  const form = new FormData();
  form.append('profile', profileFile);
  form.append('history', historyFile);
  form.append('tariffs', tariffsFile);
  form.append('seed', '42');
  const headers = { 'X-BeeSmart-Request': '1' };
  if (accessToken) headers.Authorization = `Bearer ${accessToken}`;
  let response = await fetch(`${apiBase}/api/uploads/run`, {
    method: 'POST', headers, body: form,
  });
  let run = await response.json();
  if (!response.ok) throw new Error(run.detail || 'Ошибка загрузки');
  onProgress(run);
  while (run.status === 'queued' || run.status === 'running') {
    await new Promise(resolve => setTimeout(resolve, 1000));
    response = await fetch(`${apiBase}/api/runs/${run.id}`, { headers });
    const next = await response.json();
    if (!response.ok) throw new Error(next.detail || 'Ошибка получения результата');
    run = next;
    onProgress(run);
  }
  if (run.status === 'failed') throw new Error(run.error);
  return run;
}
```

Отдельный POST `/api/runs` после загрузки не нужен. Ответ 202 означает, что CSV
прошли проверку и расчёт поставлен в очередь. Пока идёт загрузка или расчёт,
следующая попытка получает 409; кнопку запуска следует отключить до завершения.
401 — отсутствует/неверен токен; 429 — временная блокировка после серии ошибок
авторизации. При перезапуске сервера незавершённый запуск помечается `failed`:
повторите загрузку. Готовые отчёты сохраняются в постоянном хранилище.
Ошибки: 400 — некорректный multipart; 408 — таймаут передачи; 413 — превышен
размер тела; 415 — неверный Content-Type; 422 — поля/seed/содержимое CSV не прошли
проверку (включая размер отдельного файла); 503 — не удалось сохранить файлы.
Сообщение пользователю находится в `detail`.

У загруженного запуска `dataset={source:"uploaded",id,customers,history_rows,tariffs}`.
У запуска на встроенной базе — `dataset={source:"bundled"}`. `/api/overview` всегда
описывает встроенную базу; для результата загрузки используйте `run.dataset` и
`run.metrics.baseline_total_arpu`. Показывайте `campaigns`, `metrics` и события
`pilot_result`; CSV доступен по `/api/runs/{id}/submission.csv`, JSON — по
`/api/runs/{id}/report.json`. Сообщения сервера вставляйте через `textContent`.

Каждая загрузка получает свой каталог с фиксированными именами CSV. Имя файла
клиента не используется как путь. Сохраняются последние 5 наборов и 20 отчётов
локально в `work/`; эти файлы не попадают в Git. Загружаемый набор использует
официальную локальную mock-среду: показанный эффект — результат симуляции.
