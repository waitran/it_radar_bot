# Этап 3: избранное Mini App и запуск из бота

## Две независимые SQLite

- Каталог: `MINIAPP_DB_PATH` (по умолчанию `/data/bot.db`), только URI `mode=ro`,
  `query_only=ON`, короткие соединения, без миграций и изменений очередей.
- Пользовательские данные: `MINIAPP_USER_DB_PATH`; по умолчанию `miniapp.db` рядом
  с каталогом, то есть `/data/miniapp.db`. Этот каталог должен быть доступен API на запись.
  Можно разместить его на отдельном writable томе, оставив каталог read-only.

При создании API база пользователей инициализируется один раз: приватный файл 0600,
новый каталог 0700, WAL, `busy_timeout` через connect timeout 2 секунды,
короткая транзакция `BEGIN IMMEDIATE`, версия схемы `PRAGMA user_version=1`.
Инициализация повторяема и безопасна при одновременном старте нескольких workers.
Более новая или неизвестная схема отвергается. Совпадение с каталогом, его WAL/SHM/
журналом/lock, включая symlink и hardlink, запрещено до открытия writable соединения.

```sql
CREATE TABLE favorites (
    telegram_id INTEGER NOT NULL,
    item_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (telegram_id, item_key)
);
CREATE INDEX idx_favorites_user_created
    ON favorites(telegram_id, created_at DESC, item_key);
```

`created_at` — ISO 8601 UTC. Повторный PUT не меняет время сохранения. Внешнего ключа
между файлами и `ATTACH` нет. Все запросы параметризованы. Пользователь определяется
только из проверенного Telegram initData, client-side UID не принимается как основание доступа.

Пользовательский файл сохраняется после рестартов. Его нужно хранить на постоянном диске;
существующие автоматические backup бота покрывают **только bot.db**, не miniapp.db.
Для живого WAL используйте SQLite backup, а не копирование только основного файла.
Изолированный `miniapp.backend.local` специально создаёт временную пользовательскую БД,
игнорирует постоянный MINIAPP_USER_DB_PATH и удаляет её при выходе.

## API

Все три endpoint требуют `Authorization: tma <исходный Telegram initData>`:

- `GET /api/favorites?page=1&page_size=20`: `{items,total,page,page_size}`,
  размер страницы 1..50, номер 1..10000; порядок — время сохранения DESC, затем item_key.
  Запись `{item_key,created_at,event}`. `event` соответствует каталогу, `is_favorite=true`.
- `PUT /api/favorites/{id}`: сохранить существующую доступную публикацию;
  ответ `{id,is_favorite:true}`. Повтор безопасен. Неизвестная/исключённая из каталога — 404.
- `DELETE /api/favorites/{id}`: удалить только запись текущего пользователя;
  ответ `{id,is_favorite:false}`. Повтор и удаление отсутствующей записи безопасны.

`id` — существующий `seen_items.item_key`, кодируется `encodeURIComponent` в URL.
Длина ограничена 512 символами. Идентичные item_key разных пользователей независимы.
`/api/events` и `/api/events/{id}` дополнены `is_favorite` для проверенного пользователя.
Проверка авторизации прежняя; её отключение не предусмотрено.

Если публикация пропала из каталога, избранное сохраняет запись с `event:null`.
Интерфейс сообщает о недоступности и позволяет её удалить. Неизвестные данные не
восстанавливаются из предположений. При недоступной базе — 503 без служебных путей.
Снимок каталога и запись избранного независимы: исчезновение публикации сразу после PUT
может дать tombstone при следующем чтении; распределённой транзакции между файлами нет.

## Frontend

Нижняя панель: Главная / Каталог / Избранное / Профиль.
Закладки есть на карточках и в подробностях. Состояние приходит с API и синхронизируется
в памяти React между экранами; localStorage не используется. Избранное — серверные страницы
по 12 записей, с переходом в подробности и оригинал, загрузкой, ошибками, retry и пустым экраном.
После удаления последней записи страницы выполняется переход на предыдущую доступную страницу.

Выполняется не более одной мутации одного item_key одновременно, включая разные кнопки
одной публикации. Повторные нажатия блокируются синхронно. Видимое состояние меняется
после подтверждения сервера; ошибка сохраняет старое состояние и предлагает повторить.
PUT/DELETE идемпотентны, поэтому повтор после сетевого сбоя безопасен. При смене проверенного
пользователя данные в памяти и незавершённые запросы сбрасываются. «Назад» возвращает в
исходный раздел с его страницей; Telegram BackButton сохранён.

Старое `/saved` бота работает со своей существующей таблицей. Оно не синхронизируется с
избранным Mini App: основная БД остаётся read-only для API, старые записи не удаляются.

## Запуск из Telegram

Нужен действующий HTTPS URL собранного frontend, доступный Telegram, с `/api` на том же
origin. На этом этапе адрес не публикуется и production `.env` не редактируется.

Когда такой адрес будет настроен, задайте в окружении **существующего процесса бота**:

```bash
export MINIAPP_URL='https://ваш-действующий-домен/'
# Опционально: установить стандартную кнопку меню личных чатов при запуске бота.
export MINIAPP_MENU_BUTTON=1
```

`docker-compose.yml` передаёт эти две переменные существующему сервису бота;
никаких новых сервисов/polling процессов нет. Для backend дополнительно задайте:

```bash
export MINIAPP_DB_PATH=/data/bot.db
export MINIAPP_USER_DB_PATH=/data/miniapp.db
# TELEGRAM_BOT_TOKEN задаётся только в серверном окружении, не в VITE_*.
python -m uvicorn miniapp.backend.main:create_app --factory --host 127.0.0.1 --port 8000 --no-access-log
```

После обновления существующего бота пользователь отправляет `/app` и получает inline
кнопку **📱 Открыть IT Radar** с `web_app.url=MINIAPP_URL`. Та же кнопка добавлена к `/start`
и `/help` в личном чате. `/app` не включает и не выключает подписку.

Без MINIAPP_URL: `/app` объясняет, что Mini App не настроен, кнопок с фиктивной ссылкой нет,
остальные команды работают. Некорректный URL (не HTTPS, credentials, fragment, пробелы)
отклоняется конфигурацией без вывода значения. MINIAPP_MENU_BUTTON принимает только 0/1;
по умолчанию 0, optional menu API не вызывается. При включении `setChatMenuButton`
вызывается в initialize вне SQLite-блокировок; его ошибка не останавливает основной бот.

Для очереди использован уже существующий `command_replies.reply_markup` SQLite v7:
текст, JSON клавиатуры и offset сохраняются прежней транзакцией. Нового формата/миграции
очереди не требуется. Обычный scheduler отправляет кнопку, сохраняя generation checks,
429 cooldown, uncertain и изоляцию адресатов. Под SQLite-блокировкой Telegram HTTP нет.

## Проверки

```bash
# Из корня, Python 3.12 и зависимости backend установлены:
.venv/bin/python -m unittest discover -s tests -v
# Из miniapp/frontend:
npm run typecheck
npm test
npm run build
```

Новые тесты используют временные БД, синтетические initData и mocks Telegram. Проверяются
два пользователя и поддельный UID, повторные записи и удаления, рестарт, недоступная
публикация, авторизация всех методов, конкурентная инициализация, неизменность каталога
и очередей, запрещённые пути пользовательской БД, новая версия схемы, долговечная кнопка
/app, 429/uncertain, отсутствие URL, приватный чат, меню вне SQLite lock. Frontend покрывает
сохранение/удаление, быстрые нажатия, общий pending, retry, недоступные записи, смену
пользователя, переходы и состояние закладок между карточкой и подробностями.

## Изменённые файлы

- `.env.example`
- `app/config.py`
- `app/main.py`
- `app/miniapp.py`
- `docker-compose.yml`
- `docs/miniapp-stage3.md`
- `miniapp/backend/README.md`
- `miniapp/backend/config.py`
- `miniapp/backend/db.py`
- `miniapp/backend/favorites.py`
- `miniapp/backend/local.py`
- `miniapp/backend/main.py`
- `miniapp/backend/routes.py`
- `miniapp/backend/schemas.py`
- `miniapp/frontend/README.md`
- `miniapp/frontend/package-lock.json`
- `miniapp/frontend/package.json`
- `miniapp/frontend/src/App.tsx`
- `miniapp/frontend/src/api.ts`
- `miniapp/frontend/src/components/EventCard.tsx`
- `miniapp/frontend/src/components/FavoriteButton.tsx`
- `miniapp/frontend/src/favorites.tsx`
- `miniapp/frontend/src/navigation.ts`
- `miniapp/frontend/src/screens/Detail.tsx`
- `miniapp/frontend/src/screens/Favorites.tsx`
- `miniapp/frontend/src/screens/Profile.tsx`
- `miniapp/frontend/src/styles.css`
- `miniapp/frontend/src/test/api.test.ts`
- `miniapp/frontend/src/test/favorites.test.tsx`
- `miniapp/frontend/src/test/fixtures.ts`
- `miniapp/frontend/src/types.ts`
- `tests/test_miniapp_bot.py`
- `tests/test_miniapp_favorites.py`

## Результат локальной проверки

422 Python-теста, 37 frontend-тестов, TypeScript typecheck и production build прошли.
Compose-конфигурация валидна. В мобильном браузере на временных данных с настоящим
API и HMAC проверены сохранение из каталога, избранное, детали, удаление, возврат и
пустое состояние; на 320/390 px нет горизонтального переполнения.
