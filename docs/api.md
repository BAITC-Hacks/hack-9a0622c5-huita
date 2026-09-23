# API для фронтенда BeeSmart

Backend: Python 3.12+, FastAPI. Frontend: HTML, CSS, JavaScript без Node.js.
Запуск: `python -m beesmart`; адрес: http://127.0.0.1:8000.
HTML размещается в `static/index.html`, остальные файлы — в `static/`.
HTTP-слой реализован в `beesmart/api/app.py`; обработку запусков и worker выполняет
`beesmart/application/`. Расчётный агент находится в `beesmart/agent/` и вызывается
через тот же процесс выполнения из CLI. [Архитектура](architecture.md).
Локально все запросы можно отправлять на тот же origin. В production используются
HTTPS и `Authorization: Bearer <токен BeeSmart>`. Отдельный frontend-origin разрешается
через `BEESMART_ALLOWED_ORIGINS`; wildcard запрещён. Доступ внутри одной команды общий.

Машиночитаемый контракт: **[open.json](../open.json)**; живые копии на сервере:
`/openapi.json` и `/open.json`. Они доступны без авторизации и содержат только схему.
В `components/schemas` описаны `RunRecord`, `LLMRunInfo`, `AgentInfo`, `Campaign`, `Metrics`, `OverviewResponse`
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
| `GET /openapi.json`, `GET /open.json` | Публичная машиночитаемая схема API |

Для всех POST обязателен `X-BeeSmart-Request: 1`. `/api/runs` принимает JSON,
а `/api/uploads/run` — `multipart/form-data`; браузер сам выставляет его Content-Type.
Seed — целое число от 0 до 4294967295. При активном расчёте второй POST получает 409.
Статусы запуска: `queued`, `running`, `completed`, `failed`. Опрос состояния нужен
только при первых двух статусах, примерно раз в секунду. При скрытой вкладке опрос
можно остановить. `error` содержит сообщение для пользователя.

Поля запуска: `id`, `seed`, `status`, `dataset`, `llm`, `created_at`, `finished_at`, `duration_seconds`,
`events`, `campaigns`, `metrics`, `diagnostics`, `error`. Событие: `{event, data}`.
Пилоты приходят как `event="pilot_result"`. Метрики появляются после официального
локального scorer: `net_arpu_gain`, `gross_arpu_lift`, `total_cost`, `total_contacts`,
`unique_customers_targeted`, `n_pilots`, `n_final_campaigns`, `roi`, `coverage_pct`,
`budget_used_pct`, `risk_score_pct`, `status` (PASS/FAIL), `campaigns_detail`.
`roi=null` означает, что отношение не определено из-за нулевых расходов.

`run.status="completed"` означает завершённый расчёт, даже если
`run.metrics.status="FAIL"` из-за неположительного чистого прироста. `metrics=null`
до результата; `finished_at`, `duration_seconds` и `error` тоже могут быть `null`.
Отсутствующие или `null` фильтры кампании означают «все значения».

В `campaigns_detail` сначала идут пилоты, затем финал. `gross_lift` отдельной строки
считается до общей дедупликации; складывать его для получения net нельзя.
`diagnostics.gain_low` — оценка приращения финального плана, а не результат scorer.
Таблицу финала строите по `run.campaigns`: соответствующая часть детализации —
`metrics.campaigns_detail.slice(metrics.n_pilots)`, с проверкой `name` против
`campaign_name`. `n_campaigns` включает пилоты; счётчик финала — `n_final_campaigns`.
Поля с суффиксом `_pct` уже выражены в процентах. `roi` — отношение gross/затраты,
его можно показывать как `2,5×`. ARPU, расходы и прирост выражены в у.е.

В overview: `project`, `limits`, `dataset`, `runtime`, `segments`, `tariffs`,
`providers`, `files`, `agent`. `runtime.ready=false` означает, что отсутствуют необходимые
данные/модули. Готовность провайдера проверяется отдельно в `agent.ready`.
Отсутствие предустановленных CSV не запрещает загрузку: если `runtime.missing_files`
пуст, `/api/uploads/run` может работать при `runtime.ready=false`. Поле
`dataset.status` (`ready`, `missing`, `error`) относится к предустановленному набору.
Модули среды в `organizer/` и синтетические CSV получены от организаторов.
Рабочие данные хранятся отдельно от исходников BeeSmart.
Эффекты mock отличаются от скрытого судейства; результат UI не является будущим баллом.

Лимиты OpenAI и Brev/NVIDIA — по $50, раздельно. `providers[].app_spend_usd` относится
только к оценке расходов этого приложения; баланс аккаунта не считывается
(`account_balance_usd=null`). Brev/NVIDIA в текущем расчёте не используется.
OpenAI делает не более одного запроса при некэшированной подготовке.
Итеративного LLM tool calling нет. Сетевых вызовов LLM во время `Agent.act` нет: при выборе OpenAI подготовка политики
происходит до запуска worker. API-ключи не передаются браузеру или worker.

## Состояние агента и этап OpenAI

`GET /api/agent` и `overview.agent` возвращают одинаковый `AgentInfo`:

| Поля | Значение |
|---|---|
| `provider`, `engine` | `local` / `local_python` или `openai` / `openai_python` |
| `model` | `null` в локальном режиме, `gpt-6-luna` при OpenAI |
| `llm_calls`, `paid_calls` | Включён ли режим LLM и разрешены ли платные вызовы по текущей конфигурации |
| `evaluation` | Всегда `organizer_mock` |
| `ready` | Готовность конфигурации провайдера, boolean |
| `status` | `disabled`, `ready`, `needs_configuration` или `storage_error` |
| `budget_usd` | Лимит приложения, по умолчанию $5, максимум $50 |
| `estimated_spend_usd`, `reserved_usd` | Учтённая оценка расходов и незакрытые резервы |
| `request_attempts`, `completed_requests` | Попытки с резервированием и успешно принятые ответы |

Это сведения о конфигурации и локальном журнале. `ready=true` не подтверждает
доступ аккаунта к модели или достаточную внешнюю квоту. Без ключа в режиме OpenAI
или при недоступном журнале расходов запуск возвращает 503 до принятия запроса.
Обработанные ошибки этапа LLM и его таймаут после ответа 202 включают резервную
локальную стратегию.

В новых запусках `run.llm` содержит `provider`, `model`, `status`, `cache_hit`,
`input_tokens`, `output_tokens`, `estimated_cost_usd`, `reserved_usd`, `summary`,
`hypotheses`, `error_code`, `fallback_used`, `fallback_reason`. Старые сохранённые
отчёты могут не содержать `llm` или новых полей; считайте отсутствие
`fallback_used` значением `false`, а `fallback_reason` — `null`.
Статусы этапа: `disabled`, `pending`, `planning`, `cached`, `completed`, `failed`.
`run.status` описывает весь запуск, а `run.llm.status` — только подготовку политики.
После завершения LLM весь запуск ещё может находиться в `running` во время пилотов.
При `fallback_used=true` LLM сохраняет статус `failed` и исходный `error_code`, но
общий запуск продолжает работу и может закончиться `completed`. Показывайте
причину перехода и локальную стратегию, не подменяя её успешным ответом модели.
Для ошибки всего запуска используйте `run.status` и `run.error`.

| Событие | `data` |
|---|---|
| `agent_planning` | `{provider:"openai", model:"gpt-6-luna"}` |
| `agent_policy_ready` | `{cache_hit:boolean, hypotheses:number}` |
| `agent_fallback` | `{error_code:string, reason:string, policy_source:"local"}`; причина совпадает с `run.llm.fallback_reason` |
| `pilot_result` | Результат очередного пилота после подготовки политики |

При попадании в кэш новый запрос не выполняется: `cache_hit=true`, токены и стоимость
текущего обращения равны нулю. При неуспешной попытке резерв $0.02 может остаться,
поскольку расход провайдера неизвестен; резерв и учтённые расходы сохраняются и
при fallback. `summary` — недоверенное модельное обоснование
до 1000 символов; вставляйте через `textContent` и подписывайте как обоснование гипотез.
Показатели доходности берите из `metrics` после scorer. Настройки, приватность,
кэш и коды ошибок описаны в [руководстве агента](agent.md).

Production-токен BeeSmart вводит пользователь; храните его только в памяти страницы.
Не вшивайте токены в HTML/JS, Git, URL или localStorage. Ключи OpenAI/Brev фронтенду
вообще не нужны. Для GET результатов и скачиваний также нужен заголовок Authorization.
Для скачивания используйте `fetch` с этим заголовком, затем `response.blob()` и
`URL.createObjectURL`; после скачивания вызывайте `URL.revokeObjectURL`.
`/api/health` публичен и не подтверждает авторизацию. После 401 остановите polling
до ввода токена; после 429 выдержите `Retry-After` и возобновите проверку. Сетевой
сбой после POST не доказывает, что запуск не начался: сначала получите latest,
не повторяйте POST автоматически. В API нет отдельного флага `readonly`.

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
Пропуски категорий из исходной выдачи допустимы. В `customer_profile.csv` официального
пакета уже есть `predicted_arpu`; для другого набора колонку нужно подготовить заранее.
Сырые таблицы traffic/arpu_monthly этот маршрут не обрабатывает.
Нужны ячейка для пилота минимум из 10 абонентов и хотя бы один допустимый
финальный сегмент до 5 000 абонентов.

Пример для локального режима из каталога приватного пакета данных:

```bash
curl http://127.0.0.1:8000/api/uploads/run \
  -H 'X-BeeSmart-Request: 1' \
  -F 'profile=@customer_profile.csv' \
  -F 'history=@data/change_tariff.csv' \
  -F 'tariffs=@data/dict_tariff.csv' \
  -F 'seed=42'
```

Для браузера используйте `FormData` и клиент из [static/api.js](../static/api.js).
Он задаёт Authorization и служебный заголовок, обрабатывает таймауты и отмену GET.
Опрос и отображение результата находятся в [static/app.js](../static/app.js).
При смене токена отменяйте прежние запросы и очищайте данные предыдущего подключения.
После таймаута POST сначала прочитайте `/api/runs/latest`: сервер мог принять запуск.
Автоматический повтор POST может создать лишний расчёт и новый платный запрос.

Отдельный POST `/api/runs` после загрузки не нужен. Ответ 202 означает, что CSV
прошли проверку и расчёт поставлен в очередь. Пока идёт загрузка или расчёт,
следующая попытка получает 409; кнопку запуска следует отключить до завершения.
401 — отсутствует/неверен токен; 429 — временная блокировка после серии ошибок
авторизации. При перезапуске сервера незавершённый запуск помечается `failed`:
повторите загрузку. Готовые отчёты сохраняются в постоянном хранилище.
Ошибки: 400 — некорректный multipart; 408 — таймаут передачи; 413 — превышен
размер тела; 415 — неверный Content-Type; 422 — поля/seed/содержимое CSV не прошли
проверку (включая размер отдельного файла); 503 — провайдер не настроен, недоступен
журнал расходов или не удалось сохранить файлы.
Сообщение пользователю находится в `detail`.

У загруженного запуска `dataset={source:"uploaded",id,customers,history_rows,tariffs}`.
У запуска на встроенной базе — `dataset={source:"bundled"}`. `/api/overview` всегда
описывает встроенную базу; для результата загрузки используйте `run.dataset` и
`run.metrics.baseline_total_arpu`. Показывайте `campaigns`, `metrics` и события
`pilot_result`; CSV доступен по `/api/runs/{id}/submission.csv`, JSON — по
`/api/runs/{id}/report.json`. Сообщения сервера вставляйте через `textContent`.

Каждая загрузка получает свой каталог с фиксированными именами CSV. Имя файла
клиента не используется как путь. Сохраняются последние 5 наборов и 20 отчётов
в `BEESMART_STORAGE_DIR` (по умолчанию `work/`); эти файлы не попадают в Git.
Проверенные LLM-политики и журнал расходов находятся там же в `llm/`.
Загружаемый набор использует
официальную локальную mock-среду: показанный эффект — результат симуляции.
