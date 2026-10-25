# IT Radar Mini App: эксплуатация и финальная проверка

Статус: код и контейнерная конфигурация подготовлены и локально проверены. **Production
не развёрнут. Публичный сертификат не выпускался и HTTPS на реальном домене не проверен.**
Нужны сервер, домен и отдельное разрешение на production-развёртывание. Наличие Caddyfile
не означает, что сертификат уже получен. Публичного URL в результате локальной сборки нет.

## Архитектура

```text
Telegram WebView → HTTPS :443 → Caddy (собранный React / SPA)
                              └ /api/* → api:8000 (FastAPI, private network)
                                           ├ /catalog/bot.db    существующий bot volume, RO
                                           └ /miniapp-data/miniapp.db  собственный volume, RW
существующий бот → bot.db RW (отдельный Compose, единственный Telegram polling)
maintenance → SQLite backup / offline restore → отдельный volume miniapp-backups
```

`compose.miniapp.yml` — самостоятельный проект, **не override для docker-compose.yml бота**.
В нём нет бота или polling. API не публикует порт на хосте, подключён только к internal
сети и не нуждается в доступе к Telegram или интернету. Caddy подключён к private-сети
и отдельной сети для ACME. Наружу публикуются только 80/443 (внутри Caddy — 8080/8443).
HTTP перенаправляется на HTTPS. Admin API Caddy выключен, healthcheck :8081 не публикуется.

Caddy и API — non-root, read-only rootfs, cap_drop ALL, no-new-privileges, с лимитами CPU,
памяти и PID. File capability Caddy снята при сборке: для непривилегированных портов она
не нужна и мешает запуску при cap_drop ALL. Одноразовый storage-init работает без сети
и токена, видит только **новые тома Mini App/Caddy**, меняет их права на 0700/0600.
Том бота в storage-init не монтируется. API UID/GID должны совпадать с владельцем bot.db.

Образы Python 3.12, Node 22 и Caddy закреплены digest. Используются зеркала официальных
Docker Library images (Google mirror / AWS ECR Public). API содержит SQLite >=3.51.3,
проверенную при сборке; текущий образ — Python 3.12.15 / SQLite 3.53.4. Старый runtime
бота не заменён. Образы не содержат .env, тестовую SQLite, frontend/node_modules или
тестовый Caddyfile.local в runtime; web не получает токен бота.

## Возможности и API

- Проверка Telegram initData, TTL и подписанного пользователя; `/api/me`.
- Каталог `/api/events`: поиск, категории, active_only, сортировка публикаций, страницы.
- `/api/events/{id}`: подробности с раздельными датами публикации/события/дедлайна.
- `/api/favorites`, PUT/DELETE `/api/favorites/{id}`: личное идемпотентное избранное.
- Недоступная публикация остаётся удаляемой записью `event:null`.
- React: Главная / Каталог / Избранное / Профиль, темы Telegram, safe area и BackButton.
- `/app` и кнопка в `/start`/`/help`; необязательный setChatMenuButton.

`/saved` старого бота остаётся отдельной функцией: оно не синхронизируется с Mini App.
Авторизация из initDataUnsafe, query UID или cookies не принимается. Проверенные UID
берутся только из подписанного user. Backend не импортирует и не создаёт app.db.Database.

## Локальный запуск без контейнеров

Linux, Python 3.12, Node >=20.19. Из корня репозитория:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt -r miniapp/backend/requirements.txt
# TELEGRAM_BOT_TOKEN уже задан в серверном окружении; не передавать его в VITE_*.
export MINIAPP_DB_PATH=/полный/путь/к/bot.db
export MINIAPP_USER_DB_PATH=/отдельный/постоянный/каталог/miniapp.db
python -m uvicorn miniapp.backend.main:create_app --factory --host 127.0.0.1 --port 8000 --no-access-log
```

В другом терминале:

```bash
cd miniapp/frontend
npm ci
npm run dev
```

Vite проксирует `/api` на localhost:8000. Для временного snapshot каталога используйте
`python -m miniapp.backend.local --source-db /путь/bot.db`: пользовательская БД в этом
режиме также временная. Авторизация обязательна. Вне Telegram отображается экран входа,
фальшивого пользователя/авторизации нет. Для реального Telegram-клиента нужен доступный HTTPS.

## Переменные окружения

Шаблон: `miniapp/deploy/.env.example`. Скопируйте его в приватный файл **вне репозитория**, например
`/etc/it-radar/miniapp.env`, назначьте 0600 и заполните реальные значения. Существующий .env бота
не заменяйте. Не выполняйте `docker compose config` без `--quiet` с секретами в публичных логах.

| Переменная | Назначение |
|---|---|
| MINIAPP_DOMAIN | Обязательный DNS hostname, без https://, порта, пути и wildcard |
| BOT_DATA_VOLUME | Точное имя **уже существующего** named volume бота; external:true |
| BOT_UID / BOT_GID | Владелец bot.db, по умолчанию 10001:10001; API не запускается root |
| TELEGRAM_BOT_TOKEN | Токен того же бота; только API, не web и не build ARG |
| MINIAPP_API_IMAGE / MINIAPP_WEB_IMAGE | Уникальные release tags; сохранять прежние для отката |
| VITE_BOT_USERNAME | Публичный username, build ARG frontend; не токен |
| MINIAPP_AUTH_TTL | 1..86400 секунд; default 3600, будущие auth_date >30 секунд запрещены |
| MINIAPP_USER_RATE_LIMIT | Запросов проверенного UID за минуту; default 120 |
| MINIAPP_GLOBAL_RATE_LIMIT | Общий входящий лимит экземпляра (кроме health); default 600 |
| MINIAPP_API_MEMORY / CPUS | По умолчанию 256m / 0.5 |
| MINIAPP_WEB_MEMORY / CPUS | По умолчанию 128m / 0.5 |
| MINIAPP_URL / MINIAPP_MENU_BUTTON | Для существующего бота: реальный HTTPS URL и opt-in 0/1 |

В Compose пути зафиксированы: `/catalog/bot.db` RO, `/miniapp-data/miniapp.db` RW.
При запуске без Compose пути задаются MINIAPP_DB_PATH и MINIAPP_USER_DB_PATH.
MINIAPP_BIND/HTTP_PORT/HTTPS_PORT нужны только для особых сетевых схем: для публичного
ACME используйте стандартные внешние 80/443. На локальном стенде применяется отдельный override.
Production factory отвергает пустой/неверного формата токен и некорректные числовые настройки,
не выводя их значения. Разблокирующего auth bypass или переменной dev-mode нет.

## Подключение существующего каталога

Узнайте volume и владельца существующего бота, не выводя его Env с токеном:

```bash
docker inspect it-radar-bot --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Name}}{{end}}{{end}}'
docker inspect it-radar-bot --format '{{.Config.User}}'
```

Если бот имеет другое имя контейнера, используйте его. Проверьте результат, задайте
BOT_DATA_VOLUME, BOT_UID/GID. Конфигурация рассчитана на named volume текущего бота;
не создавайте пустой том с выдуманным именем. Для установки с bind mount сначала
адаптируйте read-only mount `/catalog` на staging, не копируя живую SQLite.

Read-only SQLite реально защищена на двух уровнях: mount :ro и mode=ro/query_only=ON.
Соединения короткоживущие, timeout ожидания 2 секунды, SQL progress budget около 2 секунд.
`immutable=1` не используется. Основная схема, очередь, подписки, offset и история не меняются.
При rollback journal возможны короткие read locks; абсолютное отсутствие задержек SQLite
не обещается. При WAL writer должен поддерживать читаемые существующие bot.db-wal/-shm.
При их отсутствии/перезапуске writer API может временно вернуть 503, затем восстановиться.
Не выдавайте API write-доступ к каталогу для обхода этой ошибки, не удаляйте WAL вручную.
NFS и SQLite на разных хостах этой конфигурацией не поддерживаются.

## DNS и production-развёртывание (вручную, после разрешения)

1. Подготовьте Linux-сервер с Docker Engine и Compose >=2.24.4. Убедитесь, что Docker context
   выбран правильно. Размещайте API на **том же хосте**, где находится volume бота.
2. Создайте A-запись домена на публичный IPv4 сервера. AAAA добавляйте только при работающем
   IPv6. Проверьте DNS снаружи, firewall/security group/NAT для TCP 80 и 443, корректное время/NTP.
   Caddy должен получать входящие ACME challenges; учитывайте CAA-записи домена.
3. Заполните приватный env-файл. Убедитесь, что volume существует, UID совпадает, диск свободен.
   Сделайте существующий backup bot.db штатным способом бота и пользовательский backup, если БД уже есть.
4. Сборка и проверка конфигурации **не запускают production**:

```bash
docker compose --env-file /etc/it-radar/miniapp.env -f compose.miniapp.yml config --quiet
docker compose --env-file /etc/it-radar/miniapp.env -f compose.miniapp.yml build api web
```

5. Только после разрешения на deploy:

```bash
docker compose --env-file /etc/it-radar/miniapp.env -f compose.miniapp.yml up -d --wait
```

6. Проверьте `ps`, health API/web и сертификат с внешнего компьютера:

```bash
curl --fail --show-error https://ВАШ_ДОМЕН/api/health
openssl s_client -connect ВАШ_ДОМЕН:443 -servername ВАШ_ДОМЕН -verify_return_error </dev/null
```

Проверьте trust chain, SAN нужного домена, срок действия и HTTP→HTTPS redirect.
Не используйте curl -k или обход предупреждений браузера как доказательство HTTPS.
Caddy автоматически получает/обновляет сертификаты, но ошибка DNS/ACME должна быть исправлена
до подключения пользователей. Том caddy-data хранит аккаунт ACME и приватные ключи;
не очищайте его при обновлении, защищайте его backup отдельно.

## BotFather и ручная приёмка в Telegram

После успешной внешней проверки HTTPS:

1. В окружении **существующего бота** задайте `MINIAPP_URL=https://ВАШ_ДОМЕН/`.
2. При желании `MINIAPP_MENU_BUTTON=1` настроит кнопку меню при инициализации бота.
   Перезапускайте только существующий экземпляр штатным способом; второй polling не запускайте.
3. Альтернатива: BotFather → нужный бот → Mini Apps → Menu Button:
   URL — тот же HTTPS URL, Title — «📱 Открыть IT Radar». Main App можно настроить аналогично.
4. Same-Origin Restriction оставьте включённым. Frontend и `/api` должны оставаться на одном origin.
5. В личном чате `/app` должен показать web_app-кнопку. Без URL кнопки нет, остальные команды работают.

Ручная проверка реальным пользователем обязательна: /app, проверенное имя/id в профиле,
реальные публикации, поиск/категории/страницы, неизвестные даты, сохранение/удаление,
перезапуск приложения, BackButton, темы/safe area и внешние ссылки. Вторым аккаунтом
проверьте изоляцию избранного. После TTL проверьте 401 с предложением повторного открытия.
Проверьте Menu Button/Main App на Android, iOS и Telegram Web. Никакой ручной production
приёмки Telegram или живого сбора/уведомления в ходе разработки не выполнялось.

## Backup и восстановление Mini App SQLite

Существующие backups бота не включают miniapp.db. Online backup приватной БД:

```bash
docker compose --env-file /etc/it-radar/miniapp.env -f compose.miniapp.yml run --rm --no-deps maintenance backup
# Выведется только имя нового miniapp-*.sqlite3 в /miniapp-backups.
docker compose --env-file /etc/it-radar/miniapp.env -f compose.miniapp.yml run --rm --no-deps maintenance verify /miniapp-backups/ИМЯ.sqlite3
```

Используется SQLite backup API с bounded budget 30 секунд, private mode 0600, quick_check,
проверка схемы и atomic rename законченного snapshot. Частичные файлы убираются. Настройте
регулярный запуск команды своим scheduler и копирование проверенных backup за пределы хоста.
Автоматический off-host backup/retention в этом этапе не настраивался; старые snapshots не удаляются.
Не копируйте только miniapp.db при активном WAL. Не публикуйте backup или TLS private keys.

Для восстановления обязательна остановка API. Все текущие API держат shared lifecycle lock,
restore требует exclusive lock и отказывается работать, пока API запущен. Независимо от
версии образа следуйте offline процедуре; старые образы могут не поддерживать этот guard.

```bash
docker compose --env-file /etc/it-radar/miniapp.env -f compose.miniapp.yml stop api
docker compose --env-file /etc/it-radar/miniapp.env -f compose.miniapp.yml run --rm --no-deps maintenance restore /miniapp-backups/ИМЯ.sqlite3 --replace --safety-dir /miniapp-backups
docker compose --env-file /etc/it-radar/miniapp.env -f compose.miniapp.yml start api
```

Перед заменой делается safety backup текущего состояния. Restore через SQLite backup
сохраняет атомарность, не удаляет WAL/SHM вручную и не меняет bot.db. Изменения избранного
после даты snapshot будут потеряны в восстановленной версии, но останутся в safety backup.
Возвращённая схема — v1; более новая/чужая/повреждённая БД отвергается.

## Обновление и откат

Используйте неизменяемые release tags, сохраните предыдущие API/web image IDs и приватный
env-файл. Сделайте backup, соберите новый release, проверьте локальный стенд. Обновляйте
только Mini App: `up -d --no-deps api web` с новыми image tags. Не запускайте storage-init
на работающих томах без необходимости; при смене UID сначала остановите Mini App.
Бот и его Compose проект остаются независимыми. Новых изменений схемы bot.db нет.

При ошибке остановите API/web, верните **оба прежних image tags** в приватном env-файле,
запустите `up -d --no-deps api web`, проверьте health и Telegram. Пользовательская схема
по-прежнему v1, поэтому restore обычно не нужен. При необходимости повреждённую БД
восстанавливайте offline по процедуре выше, предварительно сохранив safety backup.
Не делайте git reset --hard поверх локальных изменений, down -v или volume rm на production.
Если отключаете Mini App: сначала выключите Menu Button/Main App в BotFather и уберите
MINIAPP_URL/MINIAPP_MENU_BUTTON из окружения существующего бота, затем остановите api/web.
Тома и данные оставьте для возврата. Подписки, offset, очереди и доставленные уведомления не сбрасывайте.

## Воспроизводимые проверки

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m unittest tests.test_miniapp tests.test_miniapp_favorites tests.test_miniapp_security -v
cd miniapp/frontend
npm ci
npm run typecheck
npm test
npm run build
npm audit --audit-level=moderate
# Снова из корня:
docker build -f miniapp/backend/Dockerfile -t it-radar-miniapp-api:local .
docker build -f miniapp/deploy/Dockerfile -t it-radar-miniapp-web:local .
.venv/bin/python scripts/test_miniapp_containers.py
```

Интеграционный сценарий создаёт **уникальный тестовый проект и временный каталог**, очищает
наследуемые параметры production из окружения дочерних процессов и использует синтетический
токен. `/app`/polling он не запускает. Временный writer держит WAL-транзакцию и эксклюзивный
bot-style .lock; HTTP API одновременно читает физический RO mount. После запросов, restart,
force-recreate и backup/restore проверяется fingerprint каталога, offset и истории доставок.
Удаляются только созданные этим сценарием тома с уникальным префиксом; реальная БД не используется.
Compose local override публикует только 127.0.0.1 и **не получает сертификат**; не применяйте
его в production. Демо-публикации никогда не попадают в production image или volume.

[Финальный аудит, исправления и ограничения](miniapp-audit.md).

Источники: [Telegram validation](https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app),
[SQLite read-only WAL](https://www.sqlite.org/wal.html#read_only_databases),
[SQLite WAL-reset fix](https://www.sqlite.org/wal.html#the_wal_reset_bug),
[Caddy HTTPS](https://caddyserver.com/docs/automatic-https).

При самом первом вводе Mini App прежнего release ещё нет: безопасный откат — отключить
кнопку/URL, остановить только Mini App и сохранить его тома. Самостоятельный бот продолжит работу.
