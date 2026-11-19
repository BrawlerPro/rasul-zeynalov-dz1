# Служба поддержки — ДЗ 1 и ДЗ 2, вариант 2

Сервис ведёт заявки, переписку и статусы, сравнивает срок первого ответа с нормативом категории. Сотрудник работает со всеми заявками и сводкой; пользователь — со своими.

## Требования

Debian в WSL, Docker 29.7.2 и Compose 5.5.0. В контейнерах: Python 3.12.15, PostgreSQL 16; исходные зависимости — в `requirements.txt`, точные версии для измеренного окружения ДЗ 2 — в `load/requirements.lock.txt`. Образ ДЗ 2 добавляет py-spy 0.4.0, сохраняя версии библиотек ДЗ 1.

## Установка и запуск

```bash
git clone https://github.com/BrawlerPro/rasul-zeynalov-dz1.git
cd rasul-zeynalov-dz1
cp .env.example .env
# Задайте свои значения в .env перед запуском.
docker compose up -d --build
docker compose exec -T api python seed.py small
```

`seed.py` применяет схему и заменяет данные: `small` — 300 заявок, `work` — 50 000. Остановка: `docker compose stop`.

## Переменные окружения

| Имя | Назначение | Пример значения |
|---|---|---|
| DB_PASSWORD | Пароль PostgreSQL; Compose подставляет его в подключение | `changeme` |
| TOKEN_SECRET | Ключ подписи сессии | `changeme` |
| SEED_PASSWORD | Пароль учебных пользователей для наполнения и замерщика | `changeme` |
| DATABASE_URL | Подключение API и скриптов; Compose задаёт автоматически | `postgresql://rasul_zeynalov:changeme@db:5432/rasul_zeynalov` |

Файл `.env` не входит в Git. При повторном запуске с существующей БД сохраняйте прежний `DB_PASSWORD`; смена значения в `.env` не меняет пароль в БД.

## Проверка работоспособности

Откройте http://localhost:20802. Логин — `rasul`, пароль — заданное вами значение `SEED_PASSWORD`. После входа виден список заявок.

```bash
docker compose exec -T api python -m pytest -q
docker compose exec -T api python benchmark.py small
docker compose exec -T api python seed.py work
docker compose exec -T api python benchmark.py work
```

Замерщик делает 5 прогревочных и 30 учитываемых запросов каждой операции, выводит p50/p95/max и разложение времени. Результаты — в `results/*.json`.

## Программный интерфейс

JSON; после входа — cookie `session` или `Authorization: Bearer <token>`, срок сессии 1 час. Для POST/PATCH параметры передаются в теле, для списка — в строке запроса.

| Метод и путь | Параметры | Ответ | Ошибки |
|---|---|---|---|
| POST /api/login | login, password | 200: token, user {id, name, role}; cookie | 401, 422 |
| GET /api/categories | — | 200: items [{id, name, sla_hours}] | 401 |
| GET /api/tickets | page≥1 (1), size 1–100 (20), status? | 200: items [заявка + author, category], total, page, size | 401, 422 |
| POST /api/tickets | title, body, category_id>0 | 201: заявка | 401, 404, 422 |
| GET /api/tickets/{id} | id: целое | 200: ticket, comments, reaction_hours, sla_breached, awaiting_response | 401, 403, 404, 422 |
| PATCH /api/tickets/{id}/status | id: целое, status; только сотрудник | 200: заявка | 401, 403, 404, 409, 422 |
| POST /api/tickets/{id}/comments | id: целое, body | 201: комментарий | 401, 403, 404, 409, 422 |
| GET /api/summary | только сотрудник | 200: total, open, categories, as_of | 401, 403 |

Заявка: `id, user_id, category_id, title, body, status, created_at`; карточка дополнительно содержит `author, category, sla_hours`. Комментарий: `id, ticket_id, user_id, body, created_at`; в карточке также `author, role`. Сводка categories: `category, sla_hours, open, breached, breached_share`. Даты — ISO 8601; время реакции — часы.

`title` — 1–200 символов, `body` — 1–5000. Статусы: `new → in_progress → closed`, возврат `closed → in_progress`; комментарии к закрытой заявке запрещены. Ошибки: `detail`; 401 — вход, 403 — доступ, 404 — запись не найдена, 409 — конфликт правил, 422 — неверные параметры.

## ДЗ 2. Нагрузочное испытание

Сценарий k6 [load/dz2.js](load/dz2.js) реализует открытый поток с постоянной частотой. Два режима — смешанный профиль из восьми групп запросов и отдельно сводка `GET /api/summary`. Порог — p95 < 500 мс и доля непредвиденных ответов < 1 %. Вход сотрудника `rasul` выполняется в `setup` и исключён из метрик. Код 409 учитывается как штатный при совпадении с правилом перехода статуса или запрета комментария к закрытой заявке.

### Запуск стенда по ДЗ 1

Откройте в VS Code с подключением к WSL Debian корень сервиса ДЗ 1 с каталогом `load/` из ДЗ 2. Используйте `.env` сервиса с заданным `SEED_PASSWORD`. Если файла ещё нет, создайте его командой `cp .env.example .env` и заполните значения. Команды выполняются в терминале Debian.

```bash
export COMPOSE_PROJECT_NAME=rasul_zeynalov_dz2
export COMPOSE_FILE=compose.yaml:load/compose.load.yaml
export DZ2_K6_USER="$(id -u):$(id -g)"
export DZ2_RESULTS="$(mktemp -d "$PWD/../dz2-direct.XXXXXX")"
set -o pipefail

docker compose up -d --build
docker compose ps
curl -fsS http://127.0.0.1:20812/openapi.json -o /dev/null
printf '%s\n' "$DZ2_RESULTS"
```

Дождитесь успешного запроса `curl`. Дополнительные терминалы откройте в том же каталоге и задайте в них те же значения `COMPOSE_PROJECT_NAME` и `COMPOSE_FILE`. Для сохранения вывода в общий каталог задайте там `DZ2_RESULTS` тем путём, который напечатала последняя команда. Перед каждой ступенью `seed.py work` заново создаёт 50 000 заявок и 150 000 комментариев в отдельной базе проекта `rasul_zeynalov_dz2`.

### Ступени k6 по ЛР № 3

Перед началом каждой ступени восстановите рабочее наполнение и дождитесь спада CPU приложения и БД до уровня простоя (около 5 % одного ядра).

```bash
docker compose exec -T api python seed.py work
docker stats --no-stream "$(docker compose ps -q api)" "$(docker compose ps -q db)"
```

Выберите параметры из таблицы и выполните команду для одной ступени. Начните основную серию на 2 зап/с и удваивайте частоту до первого нарушения порога. Если сводка нарушает порог уже на 2 зап/с, продолжайте до ступени, на которой фактическая частота перестаёт расти.

| Прогон | `MODE` | `RATE` | `DURATION` | `STEP` |
|---|---|---:|---|---|
| Проверка стенда | mixed | 5 | 6s | smoke |
| Смешанный профиль | mixed | 2, 4, 8, … | 30s | mixed-2, mixed-4, … |
| Только сводка по SLA | heavy | 2, 4, 8, … | 30s | heavy-2, heavy-4, … |
| Снятие профиля сводки | heavy | 4 | 30s | profile-heavy-4 |

```bash
MODE=mixed RATE=2 DURATION=30s STEP=mixed-2
docker compose run --rm --no-deps k6 run \
  -e MODE="$MODE" -e RATE="$RATE" -e DURATION="$DURATION" \
  -e SUMMARY_PATH="/out/$STEP.summary.json" \
  --out "json=/out/$STEP.raw.json.gz" /load/dz2.js \
  2>&1 | tee "$DZ2_RESULTS/$STEP.k6.log"
```

Итоговые метрики записываются в `$DZ2_RESULTS/$STEP.summary.json`: `dz2_duration` содержит p50 (`med`), p95 и максимум; `dz2_errors` — ошибки; `dropped_iterations` — пропуски; `iterations` — фактическую частоту. В этом сценарии `handleSummary` сохраняет JSON; для просмотра результата выполните команду ниже.

```bash
python3 -c 'import json,sys; m=json.load(open(sys.argv[1]))["metrics"]; print("latency_ms:",m["dz2_duration"]["values"]); print("errors:",m["dz2_errors"]["values"]); print("iterations:",m["iterations"]["values"]); print("dropped:",m.get("dropped_iterations",{}).get("values",{}).get("count",0))' "$DZ2_RESULTS/$STEP.summary.json"
```

 Код завершения k6 99 означает нарушение порога. Для разбора ошибок сохраните журнал API сразу после ступени.

```bash
docker compose logs --since 90s --timestamps api > "$DZ2_RESULTS/$STEP.api.log"
```

### Ресурсы и профиль по ЛР № 3 и № 4

Во время ступени запустите в отдельном терминале `docker stats`. Подставьте название текущей ступени в `STEP`.

```bash
STEP=mixed-256
docker stats --format '{{json .}}' "$(docker compose ps -q api)" "$(docker compose ps -q db)" \
  | tee "$DZ2_RESULTS/$STEP.stats.jsonl"
```

В другом терминале откройте `psql` и выполните запрос с повтором каждые две секунды. `Ctrl+C` остановит опрос.

```bash
docker compose exec db psql -U rasul_zeynalov -d rasul_zeynalov
```

```sql
SELECT count(*) AS total,
       count(*) FILTER (WHERE state = 'active') AS active,
       count(*) FILTER (WHERE wait_event_type = 'Lock') AS waiting_for_lock
FROM pg_stat_activity
WHERE datname = current_database()
  AND backend_type = 'client backend'
  AND pid <> pg_backend_pid();
\watch 2
```

Для профиля запустите в первом терминале ступень `heavy` с частотой 4 зап/с. После начала нагрузки во втором терминале запишите 15 секунд работы единственного воркера Uvicorn. Проверьте версию py-spy заранее.

```bash
docker compose exec -T api py-spy --version
docker compose exec -T api py-spy record --pid 1 --duration 15 --rate 100 \
  --nonblocking --format raw --output /tmp/profile-heavy-4.txt
docker compose cp api:/tmp/profile-heavy-4.txt "$DZ2_RESULTS/profile-heavy-4.profile.txt"
```

### Опыт с двумя воркерами

Пересоздайте API с двумя воркерами и дождитесь его готовности. Повторите первую ступень смешанного профиля, которая нарушила порог. В измерениях отчёта это 256 зап/с, параметры прогона `MODE=mixed RATE=256 DURATION=30s STEP=tuning-256-w2`.

```bash
DZ2_WORKERS=2 docker compose up -d --no-deps --force-recreate api
curl -fsS http://127.0.0.1:20812/openapi.json -o /dev/null
```

После эксперимента верните один воркер.

```bash
DZ2_WORKERS=1 docker compose up -d --no-deps --force-recreate api
```

### Проверка гипотез ДЗ 1

После возвращения к одному воркеру восстановите рабочий набор, снимите планы запросов и выполните штатные тесты.

```bash
docker compose exec -T api python seed.py work
docker compose exec -T db psql -X -v ON_ERROR_STOP=1 -U rasul_zeynalov -d rasul_zeynalov \
  <<'SQL' | tee "$DZ2_RESULTS/explain.txt"
SET search_path TO rasul_zeynalov;
EXPLAIN (ANALYZE, BUFFERS)
SELECT count(*) FROM tickets t WHERE t.status='new';

EXPLAIN (ANALYZE, BUFFERS)
SELECT t.*, u.name AS author, c.name AS category FROM tickets t
JOIN users u ON u.id=t.user_id JOIN categories c ON c.id=t.category_id
WHERE t.status='new' ORDER BY t.id DESC LIMIT 20;

EXPLAIN (ANALYZE, BUFFERS)
SELECT c.*, u.name AS author, u.role FROM comments c
JOIN users u ON u.id=c.user_id WHERE c.ticket_id=12345 ORDER BY c.created_at, c.id;

EXPLAIN (ANALYZE, BUFFERS)
SELECT id, category_id, created_at, status FROM tickets;

EXPLAIN (ANALYZE, BUFFERS)
SELECT c.ticket_id, c.created_at FROM comments c JOIN users u ON u.id=c.user_id
WHERE u.role='staff' ORDER BY c.created_at;
SQL
docker compose exec -T api python -m pytest -q
```

Остановить стенд можно командой `docker compose stop`.


### Автоматический сбор полной серии

Вспомогательный Python-скрипт запускает те же 30-секундные ступени k6, восстанавливает базу перед каждой ступенью, ждёт простоя CPU, пишет Docker/PG-ряды, снимает 15-секундный профиль и выполняет опыт с двумя воркерами. Используется собранный выше образ API с py-spy.

```bash
python3 load/run.py --out "$DZ2_RESULTS" --stage all --rate 2 --duration 30 --tuning
python3 load/diagnose.py --out "$DZ2_RESULTS"
```

Веса фиксированы: список 21 %, фильтр 17 %, карточка 25 %, категории 6 %, создание 6 %, комментарий 13 %, статус 10 %, сводка 2 %. `login` выполняется в setup, поэтому в порогах используются собственные метрики рабочих запросов `dz2_duration` и `dz2_errors`. Генератор выделяет 200 VU; `dropped_iterations` означает пропущенный плановый запрос и учитывается отдельно от доли ошибок. Итерация содержит ровно один запрос.

Нагрузочный сценарий `load/dz2.js` совпадает с измеренной редакцией: SHA-256 `2b7b7eb749719eebb028ee011720240aaef4f213534faca7fbfbed4ec42bf2d8`. Исходный код сервиса: коммит `8a71612651893a956cb1539330463ef0e4fcd88d`; бизнес-логика и SQL во втором этапе не изменены.
