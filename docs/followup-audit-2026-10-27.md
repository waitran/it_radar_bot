# Follow-up IT Radar Bot — 27 октября 2026

## Исходное состояние и план (до реализации)

HEAD: `a022e873d6fa6163c57df36b9a9b91532b6258a4`, исходная ветка
`codex/audit-2026-10-27`, origin `https://github.com/waitran/it_radar_bot.git`.
Создана `codex/followup-audit-2026-10-27`; main локально отсутствует,
origin/main указывает на `3a9685d2c8a5aa9f8addc27de491e13af822a4eb`.
До работы существовали 24 изменённых tracked-файла и 9 untracked-путей.
Они сохранены, не включаются в коммиты этого этапа. Их patch и исходные копии
изменяемых файлов сохранены вне репозитория в `/tmp/it-radar-followup-baseline`.
Это означает: опубликованная ветка содержит только follow-up поверх HEAD;
предыдущие незакоммиченные v8/UX/restore-исправления требуют отдельной публикации.
Не следует развёртывать ветку без интеграции предыдущего этапа.

Прочитаны audit-2026-10-27, personal-features, beta-operations,
audit-fixes-2026-10-25 и CI workflow. AGENTS.md в каталоге проекта и родителях
не обнаружен. Реальные БД и секреты не открывались, Telegram не вызывался.

Подтверждены исходным кодом:

- A27-04, Medium: DELETE старых pending в user_control, capacity 1000.
  Новый тест с 1101 чатом до исправления: FAILED, первые 101 chat исчезли.
- A27-05, Medium: один HTTP в полёте; send_after = completion + 1,1 с;
  round-robin command/event/reminder, durable claims и generation присутствуют.
- OPS-01: штатные backup/restore для bot и Mini App существуют; health проверяет
  heartbeat, dispatcher, disk, backup age/error. VPS/offsite/получение alerts
  не подтверждены. Исторические цифры аудита не считаются текущим прогоном.

Последовательный план и критерии приёмки:

1. A27-04: bounded admission без вытеснения, coalescing по generation,
   durable counters/age, saturation/restart/recovery/isolation тесты.
2. A27-05: синтетическая матрица 100/1000/5000, обычная/смешанная нагрузка,
   429/временные ошибки/медленный HTTP, раздельные wall и model measurements;
   улучшение только при доказанном bottleneck либо документированный предел.
3. OPS-01: дополнить существующие механизмы проверкой обеих snapshots во
   временном каталоге, содержимого и terminal receipts, свежести и переходов
   unhealthy/recovery; fault injection без внешних сервисов.
4. Регрессия: актуальные команды CI, Docker без сети/non-root, frontend/API;
   ограничения среды записывать как ограничения, не как Passed.
5. Отдельные коммиты только новых изменений, secret/state scan, push в codex/…,
   проверка публикации. Main/production не изменять.

## A27-04 — Fixed, Medium

Исходный тест: 1101 chat, первые 101 принятых ответа удалялись. Теперь admission
отклоняет новый chat при 1000 строках **всех** статусов, не удаляя принятые ответы.
`user_control` возвращает bool admission; business action, settings, callback
receipt и offset фиксируются независимо от наличия места для UI-ответа.
Новый запрос того же chat заменяет generation; pending сохраняет исходный возраст
и место в FIFO. Старый generation не может завершить новый. Sending/quarantine
сохраняют прежнюю семантику, explicit новое действие того же пользователя допустимо.

Reject означает **ответ не принят**, а не обещание последующей доставки:
текстового уведомления о перегрузке нет, чтобы не создавать вторую очередь и не
отнимать HTTP-слоты уведомлений. Пользователь повторяет команду после разгрузки.
Это продуктовый компромисс, явно наблюдаемый через durable rejected counter;
callers, игнорирующие bool, по-прежнему выполняют action. Ни бесконечной очереди,
ни удаления чужого pending нет. Подписки — отдельное бизнес-хранилище, лимит 1000
относится к command outbox, не к общему числу легитимных пользователей.

`queue.commands` в health: capacity/rows/pending/oldest_age_seconds и persistent
accepted/coalesced/rejected counters. Counters имеют постоянное число ключей,
насыщаются перед SQLite int overflow; запросы ограничены размером outbox.
Quarantine может занять всю ёмкость: нужен операторский разбор, а не автоматическое
удаление uncertain. Успешная отправка освобождает место; если старая БД уже имела
>1000 terminal rows, новые чаты не принимаются до ручного разбора, без потери данных.
Тело ответа ограничено существующими Telegram/UI форматтерами; новых unbounded
структур и миграций нет. Существующие per-chat limiter и round-robin сохранены.

Новые 5 тестов: 1101 chat, recovery после освобождения слота, restart/offset/
настройки/подписка, quarantine capacity, stale generation, 8 concurrent callers,
реальные очереди команд/публикаций/напоминаний при насыщении. Все проходят.
Первый общий прогон после добавления метрик обнаружил incompatibility старого
health API; исправлен optional include_commands, старые тесты не менялись.

Файлы: app/command_admission.py, app/db.py, app/main.py,
tests/test_command_admission.py. Только новые hunks staged относительно HEAD;
предыдущие изменения этих файлов оставлены в рабочем дереве.

## A27-05 — Open / измеренный предел, Medium

Добавлен `scripts/benchmark_delivery_capacity.py`. Две матрицы по 24 сценария:
100/1000/5000 получателей × ordinary/mixed × normal/429/temporary/slow.
Mixed: N event + N/10 command + N/10 reminder; reminders создаются штатным API,
все три очереди реально проходят dispatcher и durable SQLite claims.
Network и clock замоканы: 50 ms HTTP, slow=2 s; каждый 100-й attempt в fault
сценариях возвращает retry с 30 s (429) / 10 s (503). В полёте максимум 1 HTTP.
Никаких реальных Telegram-запросов. Before использует сохранённые ДО работы
app/db.py и app/main.py; остальные существующие модули не менялись.

| Получателей / workload | До, model s | После, model s | После event wait p95, model s |
|---|---:|---:|---:|
| 100 ordinary | 121.5 | 121.3 | 115.35 |
| 1000 ordinary | 1224.9 | 1224.9 | 1163.4 |
| 5000 ordinary | 6127.299 | 6127.299 | 5820.799 |
| 5000 mixed (6000 сообщений) | 7353.499 | 7353.299 | 7046.599 |
| 5000 ordinary, 429 | 7670.249 | 7670.449 | 7273.199 |
| 5000 ordinary, slow | 15999.1 | 15998.9 | 15199.1 |

Это **не ускорение**, небольшие различия — float/virtual-clock scheduling и шум.
Реальный elapsed выполнения 5000 ordinary после: 14 837.710 ms Python+SQLite,
из них 4886.384 ms execute/commit; materialization суммарно 2433.305 ms
(пересекается с SQLite/dispatcher временем, не складывать). SQL timing исключает
cursor fetch и не является чистым SQL profiler. Измерения выполнялись на tmpfs,
одновременно с другими проверками; не характеризуют fsync/IO целевого VPS.
Initial fanout ограничен batch 500; общий capacity по-прежнему 10 000, не 500.
Материализация продолжается в dispatcher. Полные counts/statements/file sizes и
все fault/mixed значения — в двух JSON evidence.

После для 5000 ordinary slot utilization модели = 4.08%; slow = 62.5%.
Доля считается virtual HTTP / virtual elapsed, не реальная занятость CPU/сокета.
Нижняя расчётная граница при 50 ms HTTP: gap >=1.15 s, <=0.870 attempt/s;
при 2 s HTTP <=1/3.1 =0.323 attempt/s. Реальная модель медленнее из-за scheduler.
Команды увеличивают event p95 до 7046.599 s, но не вытесняют event/reminder turns.
Общий 429 останавливает все виды до retry_after; temporary retry не дублирует sent.

Выбрано документировать предел без изменения limiter/параллелизма. Ускорение
SQLite на несколько секунд не устраняет тысячи секунд pacing; данных о реальном
VPS/Telegram latency, требуемом SLO и безопасной рабочей скорости нет. Общий limiter,
per-chat, generation, claims, 429, quarantine, /stop и lock boundaries сохранены.
Лимиты Telegram API не менялись. Для ускорения владелец должен задать p95 SLO,
измерить staging latency/429 и только затем отдельно обосновать limiter/concurrency.
Не обещается throughput для реального Telegram.

Команда воспроизведения текущего состояния:

```sh
TMPDIR=/dev/shm .venv/bin/python -m scripts.benchmark_delivery_capacity
```

Harness вызывает dispatch_pending и штатный dispatch_delay (при отсутствии
метода в старом HEAD — legacy wait 2 s), не запускает Bot.run. Измеренная рабочая
версия уже включала **предыдущий незакоммиченный nearest-slot scheduler**;
опубликованная follow-up ветка его не присваивает. Поэтому её clean export с
legacy scheduler будет давать другие результаты, пока предыдущий этап не
опубликован/интегрирован. Evidence относится к исходному рабочему дереву + follow-up.
Для before в этом запуске загружены source copies из
`/tmp/it-radar-followup-baseline/{db,main}.py` через importlib, заменены
`base_main.Database=base_db.Database`, `benchmark.Bot=base_main.Bot`, вызван main.
SHA-256 сохранённых sources включены в manifest; исторические audit benchmarks
в результаты не подставлялись.

## OPS-01 — Needs Verification, эксплуатационный риск Medium

Добавлен `app.ops_verify`: age завершённого snapshot с integrity verify,
парное восстановление штатными механизмами в новый TemporaryDirectory и
streaming content comparison, local health transitions. Входные snapshots
открываются read-only. Не создаёт второй формат backup/restore, не стартует
Bot/API и не отправляет alerts. Новые 4 теста используют только synthetic DB:
оба favorites/settings/receipts, обнаружение изменения payload при тех же counts,
missing/stale/future/corrupt backup, injected ENOSPC без completed snapshot,
disk/backup/stalled dispatcher и recovery. Все проходят.

Файлы: app/ops_verify.py, tests/test_ops_verify.py,
[runbook](followup-operations-runbook.md). Runbook содержит offsite/retention,
скачивание обеих копий, RPO/RTO, независимый монитор, retry/ACK alerts,
безопасные staging fault drills и update/rollback. Локальный marker не доказывает
доставку alert; local age не доказывает offsite. Реквизитов VPS нет. Реальная
scheduled offsite, download/restore на VPS, фактические RPO/RTO и получение
оператором unhealthy/recovery остаются обязательными действиями владельца.
