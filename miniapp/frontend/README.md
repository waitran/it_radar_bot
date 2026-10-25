Финальная эксплуатационная инструкция: [docs/miniapp.md](../../docs/miniapp.md).

# IT Radar · Telegram Mini App (этапы 2–3)

Добавлены серверное избранное, кнопки закладок и четвёртый раздел навигации.
Полный контракт и запуск через `/app`: [этап 3](../../docs/miniapp-stage3.md).

React + TypeScript + Vite + Tailwind CSS 4, Telegram Mini Apps SDK `@tma.js/sdk`.
Серверное избранное с Telegram-авторизацией, без frontend-токена бота и production-демоданных.

## Локальный запуск

Нужны Node.js >=20.19 и работающий backend этапа 1 на Python 3.12.
Из корня репозитория запустите backend в первом терминале:

```bash
source .venv/bin/activate
# TELEGRAM_BOT_TOKEN уже должен быть задан в серверном окружении.
export MINIAPP_DB_PATH=/data/bot.db
python -m uvicorn miniapp.backend.main:create_app \
  --factory --host 127.0.0.1 --port 8000 --no-access-log
```

Во втором терминале, из корня репозитория:

```bash
cd miniapp/frontend
npm ci
npm run dev
```

Адрес: `http://127.0.0.1:5173`. Vite проксирует `/api` на `127.0.0.1:8000`, поэтому
CORS и изменение backend не требуются. Можно указать другой **серверный** адрес:

```bash
MINIAPP_API_TARGET=http://127.0.0.1:8765 npm run dev
```

Это удобно для изолированного backend `python -m miniapp.backend.local --source-db ...`,
который использует временный SQLite snapshot и сохраняет проверку Telegram-подписи.

При обычном открытии в браузере отображается экран входа через Telegram. Чтобы увидеть
реальные публикации и профиль, приложение необходимо открыть из Telegram с действительным
`initData`; клиент Telegram должен иметь доступ к адресу frontend (для мобильного клиента
нужен доступный HTTPS-адрес). Туннели, BotFather и production-деплой этот этап не настраивает.
Встроенного обхода авторизации, поля для произвольного ID и автологина в production нет.

Username бота по умолчанию взят из README проекта: `it_start_radar_bot`.
Для другого бота скопируйте `.env.example` в frontend `.env.local` и задайте только
публичный `VITE_BOT_USERNAME` (без `@`). Это **не токен**. Пустое/некорректное значение
скрывает переход к боту. Секреты нельзя помещать в переменные `VITE_*`.
Настоящий `.env` бота frontend не читает и не меняет.

## Экраны и фактический API

- **Главная:** имя из `/api/me`, поиск с переходом в каталог, быстрые категории,
  четыре новые публикации, переход в каталог. Дедлайны — только известные будущие,
  с незакрытой регистрацией; сортировка локальная **среди последних 12 публикаций**.
  Эта граница явно подписана: backend не поддерживает глобальную сортировку по дедлайну.
  При отсутствии достоверных дат блок скрывается.
- **Каталог:** `GET /api/events`, поиск по названию/описанию, категории
  `internships|events|contests|career`, `active_only`, сортировка `published_at asc|desc`,
  серверные страницы по 12 записей. Изменение фильтра сбрасывает страницу, возврат из
  подробностей сохраняет фильтры и страницу. Никаких вымышленных фильтров города/формата.
- **Подробности:** `GET /api/events/{encodeURIComponent(item_key)}`, заголовок, категории,
  summary, источник, публикация, дата события и дедлайн отдельно. Отсутствующие даты
  явно обозначены. Регистрация и оригинал открываются только при наличии безопасного URL.
  API не возвращает имя источника/организатора: показывается только домен либо канал
  из `url`. Даты отображаются по Москве, точность `date|time` сохраняется.
- **Избранное:** `GET/PUT/DELETE /api/favorites`, серверные страницы, сохранение и удаление,
  недоступные записи и переход к оригиналу.
- **Профиль:** только данные `/api/me`, подтверждение Telegram, информация о приложении,
  ссылка на бота. Фото/организатор/статистика не выдумываются.

Нижняя навигация: Главная / Каталог / Избранное / Профиль. History API обслуживает переходы,
системный «Назад» и прямые ссылки. Telegram BackButton подписывается и очищает обработчик
при переходах; для прямой ссылки на подробности без истории возвращает в каталог.

## Telegram, UX и безопасность

SDK получает исходный `initData`, без пересборки отправляет его как
`Authorization: tma <initData>`. `/api/me` загружается до пользовательских экранов;
сам frontend не подтверждает личность. `initDataUnsafe` не используется. Нет bot token,
записи сессии приложением в localStorage, аналитики или вывода initData в логи.

SDK вызывает ready/expand, связывает CSS-переменные `--tg-theme-*` и
`--tg-viewport-*-safe-area-inset-*`, слушает смену темы. Внешний браузер использует
системную тему и показывает вход. Есть safe area, режим reduced motion, фокус с клавиатуры,
семантические заголовки, skeleton, пустые состояния, безопасные сообщения об ошибках,
ручной retry. Поиск ограничен 200 символами и debounce 400 мс. Общая очередь ограничивает
старты запросов до одного в 250 мс, AbortController отменяет устаревшие запросы,
таймаут — 12 секунд. Автоматических повторов, создающих шторм запросов, нет.

React выводит текст как текст. `dangerouslySetInnerHTML` и автоматический HTML-парсинг
не используются. URL разрешены только HTTP(S), с запретом встроенных credentials.
В Telegram ссылки открывает SDK, в браузере — `target=_blank` с `noopener noreferrer`.
Все API-запросы идут на текущий origin `/api`, без cookies, кэша и перенаправлений.
Требование будущего размещения: маршруты SPA возвращают index.html, а `/api` обслуживается
backend на том же origin. Конфигурация production-сервера на этом этапе не добавляется.

## Проверки

```bash
npm run typecheck
npm test
npm run build
```

37 тестов Vitest + React Testing Library: список и карточки, HTML escaping, debounce,
категории/active_only/сортировка, серверная пагинация, сброс страницы, пустые состояния,
ошибки и retry, отмена устаревших ответов, переходы и «Назад», профиль, ссылки,
точность дат, будущие дедлайны, светлая/тёмная тема, SDK, исходный Authorization и
ограничение частоты запросов. Fixtures находятся только в `src/test/`, их нет в bundle.

Backend отдельно, из корня репозитория:

```bash
.venv/bin/python -m unittest tests.test_miniapp -v
.venv/bin/python -m unittest discover -s tests -v
```

Production build создаётся в игнорируемом `dist/`. Браузерная проверка выполнялась на
production bundle, с временной тестовой SQLite и настоящими `/api/me`, `/api/events`,
`/api/events/{id}` и HMAC-авторизацией. Проверены 390/320 px, смена темы через Telegram
событие, поиск, категория, сортировка, подробности, BackButton, профиль и страницы.
Тестовые публикации и симулятор Telegram не добавлены в production-приложение.

## Структура

```text
miniapp/frontend/
├── package.json / package-lock.json
├── vite.config.ts / tsconfig.json / index.html
├── .env.example / .gitignore
├── public/radar.svg
├── src/
│   ├── main.tsx / App.tsx / styles.css
│   ├── api.ts / types.ts / telegram.ts / navigation.ts
│   ├── hooks.ts / format.ts
│   ├── components/EventCard.tsx / common.tsx
│   ├── screens/Home.tsx / Catalog.tsx / Detail.tsx / Profile.tsx
│   └── test/setup.ts / fixtures.ts / *.test.ts(x)
└── README.md
```

CI запускает frontend typecheck, тесты и build. В Dockerfile бота изменён только
тестовый этап: Mini App зависимости и исходники нужны для test_miniapp; в runtime
бота они не копируются. Production-сервисы и deploy не добавлены.

Избранное реализовано на этапе 3. Production-деплой и этап 4 не выполняются.
