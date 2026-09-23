# API для фронтенда BeeSmart

Backend: Python 3.12+, FastAPI. Frontend: HTML, CSS, JavaScript без Node.js.
Запуск: `python -m beesmart`; адрес: http://127.0.0.1:8000.
HTML размещается в `static/index.html`, остальные файлы — в `static/`.
Все запросы идут на тот же origin. Приложение предназначено для локального тестирования.

| Метод и путь | Ответ |
|---|---|
| `GET /api/health` | `{ "status": "ok" }` |
| `GET /api/overview` | Профиль данных, сегменты, тарифы, лимиты, доступные CSV |
| `POST /api/runs` | Начать расчёт; JSON `{ "seed": 42 }`; ответ 202 |
| `GET /api/runs/latest` | Последний запуск; 404 если запусков нет |
| `GET /api/runs/{id}` | Состояние, события пилотов, кампании и результаты |
| `GET /api/runs/{id}/submission.csv` | Скачать финальные кампании готового запуска |
| `GET /api/runs/{id}/report.json` | Скачать отчёт |
| `GET /api/data/{file_id}` | Исходный CSV; разрешённые ссылки в `overview.files` |
| `GET /openapi.json` | Машиночитаемая схема API |

Для POST обязательны `Content-Type: application/json` и `X-BeeSmart-Request: 1`.
Seed — целое число от 0 до 4294967295. При активном расчёте второй POST получает 409.
Статусы запуска: `queued`, `running`, `completed`, `failed`. Опрос состояния нужен
только при первых двух статусах, примерно раз в секунду. При скрытой вкладке опрос
можно остановить. `error` содержит сообщение для пользователя.

Поля запуска: `id`, `seed`, `status`, `created_at`, `finished_at`, `duration_seconds`,
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

