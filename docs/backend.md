# BeeSmart — backend тарифных кампаний

FastAPI + Python 3.12, ООП-ядро `Agent.act(env)`, frontend на HTML/CSS/JavaScript без Node.js.
Контракт для напарника: [open.json](../open.json) и [описание API](api.md).

## Что делает агент

Это локальный Python-алгоритм из `beesmart/core/`: он ранжирует гипотезы по истории,
проводит пилоты, обновляет оценки и выбирает 1–10 кампаний с проверкой бюджета,
охвата и пересечений. История задаёт порядок проверки; измеренные эффекты берутся
из пилотов. Прибыль и глобальный оптимум не гарантируются.

**OpenAI и NVIDIA/Brev сейчас не используются.** `GET /api/agent` возвращает
`engine=local_python`, `model=null`, `llm_calls=false`, `paid_calls=false`.
Наличие ключа в `.env` само по себе не подключает модель. `frozen_policy.json`
содержит детерминированную базовую политику, а не результат обращения к LLM.

Оценка API выполняется в официальной локальной **мок-среде** организаторов.
Приложение не отправляет реальные маркетинговые кампании абонентам, а рассчитанный
эффект не является обещанием скрытого балла или бизнес-результата.

Brev предоставляет вычислительные инстансы; для работы LLM на нём нужно отдельно
развернуть модель и подключить inference endpoint. [Документация Brev](https://docs.nvidia.com/brev/getting-started/overview).
Лимиты аккаунтов OpenAI и Brev по $50 не считываются приложением. Текущие платные
вызовы отсутствуют. Ключи провайдеров не передаются браузеру и процессу расчёта.

## Быстрый локальный запуск

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.lock
python scripts/setup_env.py
python scripts/install_hooks.py
python -m beesmart
```

- API: http://127.0.0.1:8000
- Здоровье: `/api/health`
- Живая схема: `/openapi.json`; совместимый адрес: `/open.json`
- Схема в репозитории: `open.json`; обновление: `python scripts/export_openapi.py`

`setup_env.py` создаёт приватный `.env` один раз, с правами `0600`, генерирует
токен доступа к BeeSmart и не выводит его. Существующий файл не перезаписывается.
Для редактирования выполните `nano .env`. В Git допускается только `.env.example`
с пустыми значениями секретов. Секреты следует передавать через переменные окружения
или серверное хранилище секретов. [Рекомендации OpenAI](https://developers.openai.com/api/docs/guides/production-best-practices).

## Загрузка и расчёт

В чистом Git-клоне **нет CSV**. Возьмите три файла из локального пакета организаторов:

- `customer_profile.csv` — подготовленный профиль, включая `predicted_arpu`;
- `data/change_tariff.csv` — история переходов;
- `data/dict_tariff.csv` — справочник тарифов.

Из каталога с этими файлами:

```bash
curl http://127.0.0.1:8000/api/uploads/run \
  -H 'X-BeeSmart-Request: 1' \
  -F 'profile=@customer_profile.csv' \
  -F 'history=@data/change_tariff.csv' \
  -F 'tariffs=@data/dict_tariff.csv' \
  -F 'seed=42'
```

Ответ 202 содержит `id`. Агент стартует автоматически; состояние доступно по
`/api/runs/{id}`, итоговый CSV — `/api/runs/{id}/submission.csv`, JSON-отчёт —
`/api/runs/{id}/report.json`. В production каждому запросу к данным нужен Bearer-токен.
Подробный пример JavaScript и обработка ошибок: [api.md](api.md).

Для предустановленного набора можно указать `BEESMART_DATA_DIR` с теми же относительными
путями и использовать `POST /api/runs`. Путь к данным клиент передать не может.
Каждая загрузка сохраняется в отдельном каталоге под `BEESMART_STORAGE_DIR`.
Хранятся последние пять наборов и двадцать отчётов. Код и данные разделены;
данные не входят в Git и Docker-образ.

## Production

[Инструкция развёртывания](deployment.md): Docker Compose, постоянный том, HTTPS
через Caddy, точные разрешённые хосты/origins и отдельный токен BeeSmart.
Production предназначен для доверенной команды с общим доступом. Поддерживается
одна реплика API с одним worker; файловая блокировка не допускает второй процесс
на том же хранилище. CPU-расчёт вынесен в отдельный subprocess с таймаутом.
После сбоя незавершённые запуски получают статус `failed`; данные можно загрузить повторно.

Docker и внешний TLS не запускались на машине разработки: там нет Docker Engine.
Проверены Python-сценарии production, включая авторизацию и загрузку во внешнее хранилище.

## Проверки и защита Git

```bash
python -m pytest tests -q
python scripts/export_openapi.py --check
python scripts/check_repository.py --all
python scripts/check_repository.py --history
```

Тесты генерируют вымышленные данные и работают без CSV организаторов. Необязательный
тест локального оригинального набора пропускается, если файлов нет. Для оценки
официального набора локально доступны `python local_eval.py` и `python make_submission.py`.

Git hooks блокируют файлы данных и известные признаки секретов до коммита/push.
GitHub Actions повторяет сканирование, тесты и проверку схемы. Напарнику нужно
установить hooks после клонирования. Это дополнительные барьеры: не отключайте
их и проверяйте состав коммита. [Подробности](secrets.md).
