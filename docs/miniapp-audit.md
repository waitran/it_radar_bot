# Финальный аудит Mini App — 2026-10-25

## Область и результат

Проверены фактические FastAPI, React, интеграция с ботом и отдельный контейнерный стек.
Это аудит кода и локальной тестовой установки, **не production-приёмка**. Домен, DNS,
публичный сертификат и реальный запуск из Telegram не проверялись: сервер/домен не предоставлены.
Production .env, токены, исходная рабочая SQLite и реальные подписчики не затронуты.

## Исправлено в этапе 4

- Добавлены самостоятельные API/Caddy/React образы и Compose, external RO volume каталога,
  private network без опубликованного API, отдельные persistent user/backup/TLS volumes,
  healthchecks и ограничения CPU/RAM/PID. Старый Compose бота не заменён.
- Root initialization ограничена собственными томами Mini App; symbolic/hard links и special
  files отвергаются. Runtime non-root + read-only rootfs + drop ALL capabilities.
- На реальном контейнерном прогоне обнаружен отказ Caddy `Operation not permitted`:
  официальный binary имел cap_net_bind_service. File capability снята при сборке,
  Caddy использует высокие внутренние порты и работает без capabilities.
- Образы закреплены digest. Для пользовательского WAL выбрана SQLite 3.53.4; build assertion
  не допускает версию <3.51.3 с известным WAL-reset bug. Каталог читается только RO;
  версия SQLite/образ существующего бота не менялись.
- Устаревшие FastAPI/Starlette обновлены и закреплены вместе со всеми transitive requirements.
  certifi согласована с существующим lock бота, поэтому совместная установка не конфликтует.
- npm audit обнаружил critical/moderate в старом тестовом стеке Vitest/tinypool/mocker.
  Vitest обновлён до 4.1.12, package-lock пересчитан, тесты и build повторены.
- Добавлены process-local лимиты UID/общего потока, concurrency cap, ограничения query/path/body,
  response schemas и выдачи категорий без передачи полного payload из SQL в страницу.
- URL с credentials, неверными портами, control characters и backslash отвергаются. Слишком
  длинная ссылка не обрезается до другой кликабельной ссылки, а возвращается null.
- Строже проверяются ключи initData и цифровой auth_date. Production configuration отклоняет
  пустой/неверного формата token и безопасно скрывает значения некорректных настроек.
- Добавлены online backup и offline restore пользовательской БД. API держит shared lifecycle
  lock; restore отказывается запускаться при работающем API. Есть safety backup и проверка схемы.
- Добавлен воспроизводимый container integration script и CI job. Тестовый стенд изолирует
  наследуемые production env, использует только уникальные временные volumes и loopback.

## Контроль безопасности

| Проверка | Реализация / свидетельство |
|---|---|
| initData | HMAC-SHA256 WebAppData, compare_digest, duplicate/encoding/size checks; JSON user только после подписи |
| Срок авторизации | TTL 1..86400, default 3600; >30 секунд в будущем отвергается; нет bypass |
| Чужое избранное | Все SQL predicates используют проверенный UID; query/body UID не основание доступа; два пользователя в HTTP/unit тестах |
| SQL injection | Параметры для поиска/key/UID; sort whitelist; bounded IN placeholders |
| XSS | React text output, no dangerouslySetInnerHTML, URL allowlist; тест HTML escaping; CSP без unsafe-inline scripts |
| CORS | Same-origin `/api`, CORS middleware отсутствует, preflight чужого origin не разрешается |
| Лимиты | По 120 запросов/мин UID и 600/мин экземпляр (кроме дешёвого health); максимум 4096 UID buckets; 64 concurrent; 16KiB body; query/path 2048; q 200; page_size <=50 |
| Логи | Нет raw initData/token logging; no access logs API/Caddy; HTTP error request logger Caddy исключён; ошибки клиента не отражают ввод/пути |
| Volumes | Каталог external:true :ro; собственные тома 0700/0600, UID совпадает с ботом; source не монтируется в init/maintenance |
| Конкуренция | Короткие RO соединения, query_only, busy timeout/progress limit, никакого immutable; WAL writer с незавершённой транзакцией и exclusive bot-style lock в integration |
| Права записи | DELETE по физическому RO mount фактически отвергнут; API image не содержит app/db.py; fingerprint каталога/offset/истории неизменен |
| Сети | API только internal-сеть, порт не опубликован; admin Caddy off; HTTP localhost override отдельным файлом |
| HTTPS | Caddy production config валиден; redirects и ACME подготовлены. Реальный сертификат не получен — HTTPS не объявляется проверенным |
| Backup/restore | Online snapshot, quick_check, schema guard, offline exclusive lock, safety snapshot; протестированы restart и recreation |
| Бот | Полный unittest-набор: Telegram/website collectors, filters, dedup, delivery, 429, uncertain, subscriptions, commands, migrations, backup и health |

## Выполненные проверки

- Полный `python -m unittest discover -s tests -v`: 429 тестов прошли.
- Отдельные backend/security/favorites tests — 32 теста прошли.
- Frontend: 37 tests, TypeScript typecheck, production build — зелёные, включая Node 22 image build.
- `npm audit` — 0 известных vulnerabilities в проверенном lock.
- `pip-audit` — 0 известных vulnerabilities в 17 закреплённых API dependencies на дату проверки.
- Docker API/web build, bot test target, production Caddy config validation и Compose validation.
- Контейнерный HTTP integration: auth, filters/pages, private favorites, limits, CSP/SPA,
  физический RO catalog, работающий WAL writer, restart/force-recreate, online backup,
  отказ live restore, offline restore, неизменность каталога/polling offset/delivery history.

Результаты зависимостей относятся к текущей базе advisories, а не являются обещанием отсутствия
всех уязвимостей. Полный независимый OS/container CVE scan и внешний penetration test не выполнялись.
CI jobs добавлены, но удалённый GitHub Actions run без push в этой сессии не запускался.

## Ограничения и обязательные ручные проверки

- Нет сервера/домена: нужны реальные A/AAAA, доступные 80/443, выпуск и внешняя проверка SAN,
  цепочки и срока сертификата; только после этого BotFather/Menu Button и ручная приёмка Telegram.
- Коллекторы и Telegram отправки проверялись mocks/fixtures, без живой рассылки. Доступность
  сторонних источников и настоящие уведомления должны контролироваться штатной эксплуатацией бота.
- Одна API replica/worker: лимиты process-local, SQLite на одном локальном filesystem.
  Для горизонтального масштабирования нужен отдельный shared limiter и пересмотр хранения.
- RO WAL требует доступных sidecars. Во время restart writer возможен 503; write mount/immutable
  как обход не разрешены. При rollback journal есть короткие ограниченные read locks.
- stateless initData можно повторно использовать в пределах TTL; отдельного session revocation нет.
- `/saved` бота и Mini App favorites независимы. Глобальная сортировка по deadline отсутствует;
  главная честно показывает дедлайны только среди последней ограниченной выборки.
- Автоматический off-host backup/scheduler, алерты и disaster recovery на другом сервере не настроены;
  имеются проверенные команды и инструкция. Не используйте down -v/volume rm на production.

[Эксплуатация, команды, BotFather и откат](miniapp.md).

## Добавленные и изменённые файлы этапа 4

- `.github/workflows/ci.yml`
- `README.md`
- `app/miniapp.py`
- `compose.miniapp.local.yml`
- `compose.miniapp.yml`
- `docs/miniapp-audit.md`
- `docs/miniapp.md`
- `miniapp/backend/Dockerfile`
- `miniapp/backend/README.md`
- `miniapp/backend/auth.py`
- `miniapp/backend/config.py`
- `miniapp/backend/db.py`
- `miniapp/backend/favorites.py`
- `miniapp/backend/healthcheck.py`
- `miniapp/backend/main.py`
- `miniapp/backend/maintenance.py`
- `miniapp/backend/requirements.txt`
- `miniapp/backend/schemas.py`
- `miniapp/backend/security.py`
- `miniapp/backend/storage.py`
- `miniapp/backend/storage_init.py`
- `miniapp/deploy/.env.example`
- `miniapp/deploy/Caddyfile`
- `miniapp/deploy/Caddyfile.local`
- `miniapp/deploy/Dockerfile`
- `miniapp/deploy/Dockerfile.dockerignore`
- `miniapp/deploy/app.caddy`
- `miniapp/deploy/start.sh`
- `miniapp/frontend/README.md`
- `miniapp/frontend/package-lock.json`
- `miniapp/frontend/package.json`
- `miniapp/frontend/src/screens/Profile.tsx`
- `scripts/test_miniapp_containers.py`
- `tests/test_miniapp_security.py`
