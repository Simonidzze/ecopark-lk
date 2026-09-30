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

На странице `/admin/debtors` доступны свежие Excel-выгрузки по всем владельцам и
отдельно по тем, кто раньше платил, но перестал. Файл собирается из текущих данных
MySQL при каждом скачивании.

На странице `/admin/expenses` доступны расходы с фильтрами по периоду, статье и
строке поиска. Кнопка «Скачать Excel» формирует свежий файл из текущих данных MySQL
с учетом выбранных фильтров.

Страница `/admin/expenses/monthly` показывает расходы по завершённым календарным
месяцам, изменение к предыдущему месяцу и количество операций, статей и
контрагентов. На главной итоговый денежный баланс рассчитывается как все доступные
платежи собственников плюс прочие доходы минус все доступные расходы; это расчётный
денежный поток, а не остаток банковского счёта.

На странице `/admin/incomes` показаны прочие доходы с разбивкой по категориям и
списком банковских операций. В отчёте `/admin/debts/monthly` они добавлены к платежам
собственников в колонке «Всего доходов», но не влияют на задолженность участков.

### Прочие доходы из 1С

Endpoint 1С передаёт выбранные поступления в массиве `incomes`:

```json
{
  "incomes": [
    {
      "id": "bank:document-guid",
      "document_id": "document-guid",
      "document": "Поступление на расчетный счет",
      "date": "2026-09-22T12:00:00",
      "number": "0000-000123",
      "income_category": "Целевое финансирование",
      "counterparty_id": "counterparty-guid",
      "counterparty": "Плательщик",
      "purpose": "Целевой взнос",
      "amount": 5000.00,
      "currency": "RUB",
      "organization_id": "organization-guid",
      "organization": "ТСН МИКРОРАЙОН ЭКОПАРК",
      "source": "ПоступлениеНаРасчетныйСчет"
    }
  ]
}
```

Учитываются категории «Целевое финансирование», «Удовлетворение требований по
обращению», «Проценты по счёту» и «Возврат покупки». Платежи собственников не
дублируются в `incomes`. Если ключ `incomes` отсутствует, уже загруженные доходы
сохраняются для совместимости; пустой массив очищает их как отсутствующие в
актуальном снимке.

### Расходы из 1С

Для загрузки расходов endpoint 1С должен добавить в снимок массив `expenses`.
Каждая строка массива — отдельная позиция расходного документа:

```json
{
  "expenses": [
    {
      "id": "expense-line-guid",
      "document_id": "document-guid",
      "document": "Списание с расчетного счета",
      "date": "2026-09-22T12:00:00",
      "number": "0000-000123",
      "expense_category_id": "category-guid",
      "expense_category": "Обслуживание территории",
      "counterparty_id": "counterparty-guid",
      "counterparty": "ООО Подрядчик",
      "purpose": "Покос травы за сентябрь",
      "amount": 25000.00,
      "currency": "RUB",
      "organization_id": "organization-guid",
      "organization": "ТСН МИКРОРАЙОН ЭКОПАРК",
      "source": "1C"
    }
  ]
}
```

`id` должен быть стабильным и уникальным для позиции. Если `expenses` отсутствует,
прежние расходы сохраняются для совместимости со старой версией обмена. Если 1С
передает `"expenses": []`, ранее загруженные расходы удаляются как отсутствующие в
актуальном снимке. После обновления приложения нужно один раз выполнить
`init-schema`, чтобы создать таблицы `incomes` и `expenses`.

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
досудебной претензии в формате PDF. ФИО, участок, адрес, лицевой счет, кадастровый номер,
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
TSN_CLAIM_BASIS=01.10.2025
```

Для `TSN_CLAIM_BASIS` при пустом значении используется дата `01.10.2025`. Остальные
незаполненные реквизиты в документе явно помечаются как `не указан`, поэтому перед
направлением претензии документ нужно проверить. Исходный шаблон приложения находится в
`templates/documents/pretrial_claim.docx`, а PDF при скачивании формируется LibreOffice.
Подпись и печать добавляются в PDF автоматически из файлов
`templates/documents/assets/claim_signature.png` и
`templates/documents/assets/claim_stamp.png`; для их замены достаточно обновить эти PNG-файлы.
В Docker-образ LibreOffice уже включен. При локальном запуске вне Docker команда
`soffice` или `libreoffice` должна быть доступна в `PATH`; нестандартный путь можно задать
переменной `LIBREOFFICE_BINARY`.

## Отправка претензий через WhatsApp

Для личного номера используется отдельный локальный bridge на `whatsapp-web.js`: он
запускает WhatsApp Web в Chromium и хранит сессию в Docker volume
`whatsapp-session`. Это не официальный Business API. WhatsApp не разрешает
неофициальные клиенты и может ограничить аккаунт, поэтому отправляйте документы
только адресатам, которым ТСН вправе их направлять, и сохраняйте умеренный темп.

В Docker Compose таблица `whatsapp_messages` создаётся автоматически при старте
веб-приложения и worker через безопасный `CREATE TABLE IF NOT EXISTS`.

Для запуска достаточно собрать и поднять сервисы:

```bash
docker compose build
docker compose up -d
```

Затем откройте `/admin/whatsapp`, на телефоне выберите «Связанные устройства» →
«Привязка устройства» и отсканируйте QR-код. Сессия сохраняется после перезапуска.
Кнопка выхода на этой странице завершает сессию и показывает новый QR-код.

При старте bridge удаляет только устаревшие файлы блокировки Chromium
`SingletonCookie`, `SingletonLock` и `SingletonSocket` из профиля `session-ecopark`.
Данные авторизации при этом сохраняются. Не запускайте одновременно несколько
контейнеров `whatsapp` с одним volume `whatsapp-session`.

После события `authenticated` bridge дополнительно проверяет состояние WebSocket и
наличие функции отправки в загруженном WhatsApp Web. Это обходит ситуацию, когда
`whatsapp-web.js 1.34.7` повторяет `authenticated`, но не выдаёт событие `ready`.

Из карточки участка можно поставить одну претензию в очередь. На странице
`/admin/debtors` массовая постановка использует текущие фильтры «месяцев долга от/до»
и требует отдельного подтверждения. Для каждого участка создаётся свой PDF и свой
исходящий номер. Участки без корректного телефона, адреса или кадастрового номера
пропускаются. Доступные поля текста сообщения: `{owner}`, `{plot_number}` и
`{claim_number}`.

Очередь обрабатывается контейнером `whatsapp-worker` строго последовательно. По
умолчанию между сообщениями выдерживается 30 секунд плюс случайная пауза до 10
секунд. Результат и ошибка сохраняются в `whatsapp_messages`; неуспешную отправку
можно вручную вернуть в очередь на странице WhatsApp.

```env
WHATSAPP_SERVICE_URL=http://whatsapp:3000
WHATSAPP_AUTO_INIT_SCHEMA=true
WHATSAPP_SERVICE_TOKEN=
WHATSAPP_REQUEST_TIMEOUT_SECONDS=60
WHATSAPP_GATEWAY_IP=
WHATSAPP_GATEWAY_MODE=resolver
WHATSAPP_GATEWAY_PORT=443
WHATSAPP_GATEWAY_HOSTS=web.whatsapp.com
WHATSAPP_QUEUE_POLL_SECONDS=5
WHATSAPP_SEND_INTERVAL_SECONDS=30
WHATSAPP_SEND_JITTER_SECONDS=10
WHATSAPP_MAX_ATTEMPTS=1
```

Если блокировка выполняется по TLS SNI или фиксированный туннель `ssh -L` приводит
к `auth timeout`, используйте динамический SOCKS5-туннель. Он проксирует весь трафик
Chromium, включая дополнительные домены, DNS и WebSocket WhatsApp:

```bash
ssh -N -T -g \
  -D 172.17.0.1:19443 \
  -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=30 \
  -o ServerAliveCountMax=3 \
  USER@194.87.219.122
```

Проверка туннеля с Docker-хоста:

```bash
curl --socks5-hostname 172.17.0.1:19443 -I https://web.whatsapp.com/
```

Настройки Compose `.env` для этого режима:

```env
WHATSAPP_GATEWAY_IP=172.17.0.1
WHATSAPP_GATEWAY_MODE=socks5
WHATSAPP_GATEWAY_PORT=19443
WHATSAPP_GATEWAY_HOSTS=
```

В режиме `socks5` список `WHATSAPP_GATEWAY_HOSTS` не используется: Chromium отправляет
через SSH-прокси все HTTP(S)- и WebSocket-соединения, а DNS выполняется на SSH-сервере.

Если `web.whatsapp.com` недоступен напрямую, можно направить его на прозрачный TCP-шлюз.
Создайте рядом с `docker-compose.yml` файл `.env` (это именно Compose `.env`, не
`env.prod`):

```env
WHATSAPP_GATEWAY_IP=203.0.113.10
WHATSAPP_GATEWAY_MODE=resolver
WHATSAPP_GATEWAY_PORT=8443
WHATSAPP_GATEWAY_HOSTS=web.whatsapp.com
```

Chromium перенаправит соединение `web.whatsapp.com:443` на указанные IP и порт
(`203.0.113.10:8443` в примере), но сохранит имя
`web.whatsapp.com` в TLS SNI и при проверке сертификата. Пример шлюза Nginx:

```nginx
stream {
    resolver 1.1.1.1 ipv6=off valid=300s;

    map $ssl_preread_server_name $whatsapp_upstream {
        web.whatsapp.com    web.whatsapp.com:443;
        static.whatsapp.net static.whatsapp.net:443;
        default             web.whatsapp.com:443;
    }

    server {
        listen 8443;
        ssl_preread on;
        proxy_connect_timeout 15s;
        proxy_timeout 1h;
        proxy_pass $whatsapp_upstream;
    }
}
```

Ограничьте доступ к настроенному порту шлюза (`8443` в примере) по IP сервера
Ecopark. Если вслед за
`web.whatsapp.com` окажется недоступен `static.whatsapp.net`, добавьте его через
запятую в `WHATSAPP_GATEWAY_HOSTS`. После изменения `.env` пересоздайте bridge:

```bash
docker compose up -d --build --force-recreate whatsapp
```


Bridge доступен только внутри сети Docker и не публикует порт на хост. Volume с
сессией WhatsApp является чувствительным: не копируйте его и не передавайте третьим
лицам. Если задаётся `WHATSAPP_SERVICE_TOKEN`, тот же токен должен быть передан
контейнеру `whatsapp` через окружение Docker Compose.

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
