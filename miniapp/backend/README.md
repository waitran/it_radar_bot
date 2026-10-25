Финальная эксплуатационная инструкция: [docs/miniapp.md](../../docs/miniapp.md).

# Backend Mini App — этапы 1–3

Избранное хранится в отдельном `MINIAPP_USER_DB_PATH` (по умолчанию `/data/miniapp.db`).
API-процессу нужен writable доступ к этой базе. Подробности endpoints, схемы и
интеграции с ботом: [этап 3](../../docs/miniapp-stage3.md).

Запускать из корня `it_radar_bot`, Python 3.12:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt -r miniapp/backend/requirements.txt
# TELEGRAM_BOT_TOKEN задаётся только в окружении backend; .env не читается.
export MINIAPP_DB_PATH=/data/bot.db
python -m uvicorn miniapp.backend.main:create_app --factory --host 127.0.0.1 --port 8000 --no-access-log
```

`TELEGRAM_BOT_TOKEN` обязателен; приложение без него не создаётся. `MINIAPP_AUTH_TTL`
по умолчанию 3600 секунд, допустимо 1..86400. Dockerfile — отдельный необязательный
образ Python 3.12 (контекст сборки — корень репозитория); compose бота не изменён.
Никакого запуска бота, миграций, сетевых обращений к Telegram или рассылок API не делает.

## Контракт

- `GET /api/health`: публичный readiness, читает таблицу публикаций; 503 при недоступной БД.
- `GET /api/me`: только проверенные поля пользователя Telegram; подписка бота не требуется.
- `GET /api/events`: `{items,total,page,page_size}`.
- `GET /api/events/{id}`: подробности; `id` — существующий `seen_items.item_key`,
  URL-кодировать при подстановке в путь. Неизвестный или отклонённый объект — 404.

Пользовательские запросы требуют заголовок `Authorization: tma <Telegram.WebApp.initData>`.
Параметры каталога: `q` (до 200 символов, literal Unicode case-insensitive поиск по title/summary),
`category` (`internships`, `events`, `contests`, `career`), `active_only` (default false),
`sort=asc|desc` (default desc), `page=1..10000`, `page_size=1..50` (default 20).
Сортировка именно по `published_at`, отсутствующие даты в конце, равные даты — по item_key.
Все фильтры применяются до пагинации; count и страница читаются в одной транзакции.
Отклонённые публикации без принятой категории не попадают в каталог. Категории берутся
из сохранённого payload с fallback на category, включая несколько категорий.

Публичные поля: id, title, summary, url, apply_url, categories, published_at,
event_at, deadline_at, event_precision, deadline_precision, registration_status.
Title ограничен 512 символами, summary — 8000, каждая ссылка — 2048; не возвращаются
raw_text, source_key, локальные файлы, очереди, Telegram message IDs или payload.
URL разрешены только HTTP(S). Размер страницы ограничен 50 объектами, выполнение SQL —
примерно двумя секундами (progress handler), ожидание блокировки — двумя секундами.
Ошибки: 401 авторизация, 404 объект, 422 параметры, 503 БД/некорректные сохранённые данные.
Ответы `Cache-Control: no-store`; ошибки не отражают входные значения или пути.

## Даты и актуальность

`published_at` — дата публикации из seen_items; никогда не заменяется first_seen_at.
Дата события и дедлайн читаются из существующего event_metadata, вместе с точностью
`date|time`. Ничего не выводится автоматически из даты публикации.
Для старой схемы без индекса event_metadata даты события/дедлайна — null, статус unknown.
`active_only=true` означает: есть хотя бы одна известная будущая дата события/дедлайна,
ни одна известная дата не истекла, регистрация не closed. Unknown status допускается
при будущей дате, но не означает подтверждённо открытую регистрацию.
Без дат актуальность неизвестна: запись доступна в общем каталоге, но не в active_only.
Это снимок сохранённых данных, API не выполняет повторную проверку источника.

## Авторизация и SQLite

HMAC-SHA256 по официальному алгоритму Telegram: ключ WebAppData, сообщение bot token;
второй HMAC по отсортированным декодированным парам, исключая hash (signature остаётся).
Сравнение через compare_digest. После подписи проверяется auth_date (TTL и не более
30 секунд в будущем), затем JSON user с положительным целочисленным ID до 52 бит.
Дублированные поля, неверная кодировка, отсутствующая подпись и слишком длинные данные
отклоняются. initDataUnsafe и query ID не используются. Сырые initData и секреты не логируются.
Авторизацию нельзя отключить переменной окружения; docs/openapi не выставлены наружу.

API не импортирует app.db.Database: отдельные короткоживущие sqlite3-соединения,
URI `mode=ro`, `query_only=ON`, связанные параметры SQL, фиксированные варианты сортировки.
Никаких DDL/записей, immutable=1 или изменения journal_mode. WAL виден вместе с
зафиксированными записями writer; незавершённые транзакции writer не видны.
Доступ нужен к самому файлу и его каталогу с WAL/SHM, под UID/GID с необходимыми правами
(бот создаёт БД 0600). Нельзя копировать только bot.db при активном WAL: использовать
SQLite backup. На read-only mount SQLite должен иметь доступ к уже существующим WAL/SHM;
создание/обслуживание служебных WAL-файлов — особенность SQLite, не запись таблиц API.

## Изолированный локальный режим

Отдельная команда создаёт временный согласованный snapshot через SQLite backup,
читая исходник с mode=ro. Сервер слушает только 127.0.0.1:8765, snapshot удаляется при выходе:

```bash
python -m miniapp.backend.local --source-db /path/to/bot.db
```

Проверка Telegram остаётся обязательной. Production factory не содержит переключателя
local/dev или auth bypass, поэтому этот режим нельзя включить переменной окружения.
Для полностью автономных тестов без Telegram и токена используйте временные fixtures и
синтетические подписанные initData в тестах (их нет в рабочей конфигурации):

```bash
python -m unittest tests.test_miniapp -v
python -m unittest discover -s tests -v
```
