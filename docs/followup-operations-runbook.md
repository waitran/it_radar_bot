# OPS-01: локальные инструменты и приёмка владельцем

Это дополнение к существующим `app.backup`, `miniapp.backend.maintenance` и
`app.healthcheck`, без нового формата snapshot и без второго механизма restore.
Команды выполняются из проверенного checkout с зависимостями bot + Mini App.
Ниже placeholders; никаких production-команд в ходе аудита не выполнялось.

## Проверка завершённых локальных копий

```sh
python -m app.ops_verify age /path/to/bot-backups --kind it-radar --max-age 93600
python -m app.ops_verify age /path/to/miniapp-backups --kind miniapp --max-age 93600
python -m app.ops_verify drill-pair /path/to/bot-snapshot.sqlite3 /path/to/miniapp-snapshot.sqlite3
```

Age проверяет newest managed non-symlink snapshot по mtime и целостность;
`.part` не считается успешной копией. Повреждённая последняя копия не маскируется
предыдущей. Это проверка локального completion time, а не offsite freshness.
Pair drill использует штатный restore в новом TemporaryDirectory, удаляет его
после проверки. Сравнивает содержимое (streaming SHA-256), а не только counts:
пользователи/настройки/избранное/aliases/payload/terminal receipts/offset/cursors
и отдельное Mini App favorites. В выводе только aggregate counts, без ID/данных.
Преобразования pending/sending при restore намеренно не сравниваются как
immutable receipts. Совместимый snapshot должен иметь personal schema v7+.
Две БД не являются общей транзакцией: выбрать близкие по времени snapshots,
после восстановления проверить orphaned Mini App favorites отдельно.

Для больших копий выделить место минимум под обе восстановленные БД и их
SQLite journal/WAL, выставить TMPDIR на закрытый каталог с достаточной ёмкостью;
ограничить runtime/CPU/RAM штатным systemd/контейнером. Drill не изменяет входные
копии, но они содержат приватные данные: права 0600, каталог 0700, без публикации
в GitHub/логах. Эти инструменты не принимать за проверку production по unit-тестам.

## Offsite и retention

1. Штатный bot backup: `python -m app.backup create --db /data/bot.db --dir /backups --keep 7`.
2. Штатный Mini App backup: `python -m miniapp.backend.maintenance --db /miniapp-data/miniapp.db --catalog /catalog/bot.db backup --dir /miniapp-backups`.
3. Отправлять только завершённые snapshots в независимое зашифрованное хранилище
   через уже выбранный оператором restic. Credentials — вне репозитория, 0600;
   password/recovery key также вне VPS. Сначала проверить обе age-команды.
4. После успешной загрузки `restic check`; retention для помеченных snapshots:
   `restic forget --tag it-radar --keep-daily 7 --keep-weekly 4 --keep-monthly 6 --prune`.
   Не применять к live volumes. Для Mini App локальный CLI не имеет retention:
   операторский job удаляет только завершённые `miniapp-*.sqlite3`, проверенные и
   уже загруженные offsite, сохраняя минимум 7 последних и safety copies отдельно.
5. Еженедельно `restic check --read-data`; ежемесячно скачать обе БД через
   `restic restore SNAPSHOT_ID --target NEW_EMPTY_DIRECTORY`, запустить drill-pair.
   Измерять RPO по последней успешной внешней загрузке, RTO по полному скачиванию,
   restore, проверке и запуску. Локальные mtime не доказывают успешный offsite.

## Alerts без реальной отправки в тестах

```sh
python -m app.healthcheck /path/to/bot.health.json
python -m app.ops_verify health-transition /path/to/bot.health.json /private/monitor/transition.json
```

Монитор должен работать вне bot container и видеть его остановку: период 30–60 с,
alert на nonzero health/age/backup/offsite/drill job; recovery после успеха.
Не перезапускать unhealthy автоматически и не запускать второй poller.
Health обнаруживает stale heartbeat/dispatcher/polling, source budget, старую
очередь, disk threshold, backup missing/error/stale. Дополнительно алертить на
рост `queue.commands.rejected`, capacity, возраст pending/sending; занятый
quarantine требует ручного разбора. Минимум свободного диска не уменьшать.

`health-transition` печатает `unhealthy`/`recovery` один раз на изменение и
сохраняет только локальный marker. Это источник диагностических событий, не
транспорт доставки: мониторинг должен самостоятельно надёжно повторять
неполученные оператором alerts и проверять их acknowledgement. Успешное
сохранение marker не означает, что оператор получил сообщение. Не подключать
webhook/платный сервис без разрешения владельца.

## Обязательная приёмка на целевом VPS — Needs Verification

Заполнить журнал: UTC timestamp, commit/image digest, результаты команд,
offsite snapshot ID без credentials, фактические RPO/RTO, ACK оператора.

- Успешная scheduled backup обеих БД, offsite, проверка retention и скачивание.
- Pair drill скачанных копий; убедиться в users/preferences/saved/favorites,
  terminal receipts; запускать только отдельный staging bot и staging volumes.
- В отдельном тестовом volume/instance смоделировать ENOSPC, denied backup path,
  остановку dispatcher/heartbeat; получить реальный unhealthy, затем recovery.
  Не заполнять production-диск и не портить рабочую БД ради теста.
- Проверить монитор при остановленном bot container и недоступном VPS,
  отсутствие пропуска alerts при отказе транспорта.
- Проверить настройки, меню, /stop → /start и отсутствие архивного replay
  двумя тестовыми аккаунтами. Реальный Telegram staging load test — отдельно.

В этом задании нет реквизитов/доступа VPS; ни один пункт выше не отмечен выполненным.

## Обновление и откат

Сначала опубликовать и интегрировать предыдущий незакоммиченный этап (v8/menu/
restore fixes) отдельно от follow-up. Проверить объединённый commit в staging,
выполнить CI, сохранить прежний image digest/config и проверить обе snapshots.
Остановить единственного bot writer и API, сохранить volumes; запустить новую
сборку с прежними секретами/volumes. Проверить health, queue counters, UI и receipts.
Не использовать `down -v`. Follow-up не добавляет миграций и не меняет лимиты API.

Для отката остановить новую версию, сохранить свежие snapshots для разбора,
вернуть прежний **совместимый со схемой** image/config. Если требуется старый
schema v7 image после v8, использовать проверенную pre-upgrade копию через штатный
restore с `--replace --safety-dir`, сохранив текущее состояние; не понижать PRAGMA
вручную. Restore откатывает offset и новые пользовательские изменения, pending
доставки остаются в quarantine/review; вручную сверить период после snapshot.
Exactly-once `sendMessage` не гарантируется. Деплой/откат делает владелец.
