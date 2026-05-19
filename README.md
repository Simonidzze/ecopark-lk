# Ecopark LK sync

Python-скрипт создает таблицы MySQL через SQLAlchemy ORM-модели и синхронизирует данные из 1С API:

```text
GET /ecopark/hs/ecopark-sync/snapshot
```

## Установка без Docker

```bash
cd /Users/simon/work/ecopark/lk
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example env.prod
```

Заполнить `env.prod`: доступ к MySQL, HTTP-сервису 1С и Google Sheets.
Файл `env.prod` не коммитится в git.

## Создать схему

База `MYSQL_DATABASE` должна существовать в MySQL, а пользователь из `.env` должен иметь права на создание таблиц.

```bash
. .venv/bin/activate
python ecopark_sync.py --env-file env.prod init-schema
```

Команда вызывает `Base.metadata.create_all(...)`, структура описана классами в `ecopark_sync/models.py`.

## Разовая синхронизация

Из 1С API:

```bash
. .venv/bin/activate
python ecopark_sync.py --env-file env.prod sync
```

Из сохраненного JSON для проверки:

```bash
. .venv/bin/activate
python ecopark_sync.py --env-file env.prod sync --from-file /path/to/ecopark_sync_snapshot.json
```

## Расписание

По умолчанию используется ежедневное расписание в часовом поясе Новосибирска:

```env
SCHEDULE_MODE=daily
SCHEDULE_TIMEZONE=Asia/Novosibirsk
SYNC_DAILY_AT=13:00
GOOGLE_SHEETS_DAILY_AT=13:10
SYNC_EXPORT_AFTER_SYNC=false
```

В `13:00` запускается синхронизация из 1С в MySQL, в `13:10` запускается выгрузка в Google Sheets.

Запуск отдельным worker-процессом:

```bash
. .venv/bin/activate
python ecopark_sync.py --env-file env.prod scheduler
```

Ошибки синхронизации пишутся в `sync_runs`, если MySQL доступен.

## Интервальный scheduler

Встроенный планировщик запускает синхронизацию в цикле внутри Python-процесса.
Если нужен старый интервальный режим, задайте:

```env
SCHEDULE_MODE=interval
SYNC_INTERVAL_SECONDS=86400
SYNC_RUN_ON_START=true
SYNC_START_DELAY_SECONDS=10
```

## Flask admin

Минимальный веб-интерфейс администратора:

```bash
. .venv/bin/activate
python ecopark_sync.py --env-file env.prod web
```

По умолчанию сервер слушает:

```text
http://127.0.0.1:8080/admin
```

Настройки:

```env
FLASK_HOST=127.0.0.1
FLASK_PORT=8080
FLASK_DEBUG=false
WEB_SYNC_ENABLED=true
```

Если `WEB_SYNC_ENABLED=true`, Flask сам запускает фоновый планировщик.

## Google Sheets

Экспорт в Google Sheets использует service account. Нужно создать JSON-ключ и расшарить таблицу на email сервисного аккаунта.

```env
GOOGLE_SHEETS_EXPORT_ENABLED=true
GOOGLE_SHEETS_SPREADSHEET_ID=spreadsheet-id
GOOGLE_SHEETS_WORKSHEET=Участки
GOOGLE_SERVICE_ACCOUNT_FILE=/run/secrets/google-service-account.json
```

Ручной экспорт:

```bash
. .venv/bin/activate
python ecopark_sync.py --env-file env.prod export-sheets
```

## Обзвоны

CSV-отчет обзвона можно загрузить в админке на странице `/admin/calls` или через CLI:

```bash
. .venv/bin/activate
python ecopark_sync.py --env-file env.prod import-calls /path/to/report.csv
```

Аналитика сопоставляет телефоны из отчета с текущими владельцами участков и показывает оплаты, которые появились после звонка.

## Docker Compose

Перед запуском положить реальные настройки в `env.prod`, а JSON-ключ service account в `google-service-account.json`.

```bash
docker compose build
docker compose up -d
```

Приложение будет доступно на:

```text
http://127.0.0.1:8080/admin
```

Создать схему из контейнера:

```bash
docker compose run --rm ecopark-lk python ecopark_sync.py init-schema
```
