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

Аналитика сопоставляет телефоны из отчета с текущими владельцами участков и показывает оплаты,
которые появились после звонка и до следующего обзвона. Для последнего обзвона учитываются
оплаты до текущего момента.
Дата обзвона и день группировки отчетов берутся из четвертой колонки `Дата создания`
строки `Рассылка`.

## Досудебные претензии

На карточке участка `/admin/plots/<owner_plot_id>` есть форма скачивания заполненной
досудебной претензии в формате DOCX. ФИО, участок, адрес, лицевой счет, кадастровый номер,
задолженность, пени и дата расчетного снимка берутся из MySQL. Начало периода долга по
умолчанию — `01.10.2025`. Исходящий номер присваивается автоматически при скачивании из
сквозной последовательности и сохраняется в журнале `pretrial_claims`. Период долга и
реквизиты основания можно уточнить перед скачиванием.

Кадастровый номер хранится в `plots.cadastral_number` и заполняется отдельно разовым
импортом. Синхронизация из 1С это поле не изменяет и не очищает. Если значение пустое,
форма пробует найти кадастровый номер в адресе участка и позволяет ввести его вручную.
При синхронизации адреса участков очищаются от лишних обратных слешей и прямых кавычек,
которые могут присутствовать в строках 1С.

Постоянные реквизиты ТСН задаются в `env.prod`:

```env
TSN_LEGAL_ADDRESS=633204, Новосибирская область, г. Искитим, ул. Карбышева, д. 16
TSN_PHONE=+7 961 846-52-56
TSN_EMAIL=tsn-ecopark@mail.ru
TSN_CLAIM_BASIS=
```

Если значение отсутствует, в документе явно печатается `не указан`, поэтому перед
направлением претензии документ нужно проверить. Исходный шаблон приложения находится в
`templates/documents/pretrial_claim.docx`.

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
