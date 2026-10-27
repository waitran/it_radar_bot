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
