# Служба поддержки — ДЗ 1, вариант 2

Сервис ведёт заявки, переписку и статусы, сравнивает срок первого ответа с нормативом категории. Сотрудник работает со всеми заявками и сводкой; пользователь — со своими.

## Требования

Debian в WSL, Docker 29.7.2 и Compose 5.5.0. В контейнерах: Python 3.12.15, PostgreSQL 16; версии библиотек — в `requirements.txt`.

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
