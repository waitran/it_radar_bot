"""it-radar-bot: Telegram-бот, который следит за возможностями для студентов и
начинающих IT-специалистов в заданных источниках (Telegram-каналы через t.me/s/ и
сайты компаний) и присылает подписчикам новые подходящие объявления.

Запуск:  python -m app.main          (токен — из переменных окружения)
Команды: /start — включить рассылку, /stop — выключить, /status — личная подписка.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import signal
import threading
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

from app.collectors import croc, k2, rtkit, telegram_channel
from app.config import Config, ConfigurationError
from app.db import Database
from app.filters import Filters
from app.models import Item
from app.notify import TelegramNotifier, BotAPIError
from app.formatting import format_message, escaped_clip
from app.logging_config import configure_logging

log = logging.getLogger("main")

WEBSITE_COLLECTORS = {
    "k2_vacancy": k2.K2Collector,
    "croc_vacancies": croc.CrocCollector,
    "rtkit_careers": rtkit.RtkitCollector,
}


class Bot:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.db = Database(cfg.db_path)
        self.notifier = TelegramNotifier(cfg.bot_token, cfg.owner_id)
        self.filters = Filters(cfg.rules)
        # Флаг рассылки: /start включает, /stop выключает. Сохраняется в БД — переживает рестарт.
        self._lock = threading.RLock()
        self.stop_event = threading.Event()
        self.scan_requested = threading.Event()
        self._scan_running = False
        # A read-only check interrupted by shutdown can be resumed safely;
        # publication delivery still uses the existing persisted state machine.
        if self.db.get_flag("check_in_progress") == "1":
            self.db.set_flag("check_pending", "1")
            self.db.set_flag("check_in_progress", "0")
        self.fatal_error = None
        self.bot_username = ""
        self._fresh_items = {}
        self._available_sources = set()
        self._last_scan = 0.0
        try:
            self.db.migrate_owner(cfg.owner_id)
        except Exception:
            self.db.close()
            raise

    def is_enabled(self) -> bool:
        return self.db.has_users(active_only=True)

    def ever_started(self) -> bool:
        return self.db.has_users()

    is_enabled_ever = ever_started

    def set_enabled(self, on: bool) -> None:
        """Compatibility helper for administrative owner; commands always use sender ID."""
        if self.cfg.owner_id > 0:
            with self._lock:
                self.db.user_control(self.cfg.owner_id, active=on)

    def initialize(self):
        me = self.notifier.call("getMe")
        info = self.notifier.call("getWebhookInfo")
        if not isinstance(me, dict) or not isinstance(me.get("username"), str) or type(me.get("id")) is not int:
            raise RuntimeError("Unexpected getMe response")
        if not isinstance(info, dict) or info.get("url"):
            raise RuntimeError("Webhook configured or invalid response; remove webhook explicitly before long polling")
        self.db.bind_identity("bot_id", me["id"])
        self.bot_username = me["username"].lower()

    def service_message(self, text, telegram_id=None):
        telegram_id = self.cfg.owner_id if telegram_id is None else telegram_id
        if telegram_id <= 0:
            return
        with self._lock:
            if self.stop_event.is_set():
                return
            delay = float(self.db.get_flag("send_after", "0")) - time.time()
            if delay > 1.1:
                return  # A server-imposed rate limit: do not block command handling.
            if delay > 0 and self.stop_event.wait(delay):
                return
            self.db.set_flag('send_after',str(time.time()+1.1))
            result = self.notifier.send(escaped_clip(text, 3800), chat_id=telegram_id)
            if result.status == "sent":
                self.db.set_flag("send_after",str(time.time()+1.1))
            if result.status == "retry" and result.code == 429:
                self.db.set_flag("send_after",str(time.time()+result.retry_after))
            if result.code == 403:
                self.db.user_control(telegram_id, active=False)
            if result.code == 401:
                self.fatal_error = "Telegram token rejected"
                self.stop_event.set()
            return result

    def flush_command_reply(self):
        with self._lock:
            if self.stop_event.is_set() or float(self.db.get_flag('send_after','0')) > time.time():
                return
            row = self.db.pending_reply(time.time())
            if not row:
                return
            self.db.set_reply_state(row['telegram_id'], 'sending')
            try:
                result = self.service_message(row['text'], telegram_id=row['telegram_id'])
            except Exception:
                self.db.set_reply_state(row['telegram_id'], 'uncertain')
                raise
            if result is None:
                self.db.set_reply_state(row['telegram_id'], 'pending')
            elif result.status == 'retry':
                self.db.set_reply_state(row['telegram_id'], 'pending', time.time()+max(10,result.retry_after))
            else:
                self.db.set_reply_state(row['telegram_id'], result.status)

    def check_request_reply(self, now):
        """Called with the command lock held; accepting is persisted with offset."""
        if self._scan_running:
            return False, "Проверка уже выполняется. Вторую параллельно не запускаю."
        if self.db.get_flag("check_pending") == "1":
            return False, "Проверка уже запрошена и скоро начнётся."
        remaining = 60 - (now - float(self.db.get_flag("check_last_request", "0")))
        if remaining > 0:
            return False, f"Повторный /check доступен через {math.ceil(remaining)} сек."
        return True, ("Проверка запрошена. Проверю источники и пришлю итог. "
                      + ("Новые подходящие объявления поступят через очередь." if self.is_enabled()
                         else "Рассылка выключена: пришлю только итог, без объявлений."))

    def flush_check_summary(self):
        with self._lock:
            if self.stop_event.is_set() or self.db.get_flag("check_summary_pending") != "1":
                return
            if float(self.db.get_flag("send_after", "0")) - time.time() > 1.1:
                return
            # Claim before HTTP: an ambiguous response must not duplicate reports.
            self.db.set_flag("check_summary_pending", "0")
            try:
                result = self.service_message(self.db.get_flag("check_summary", "Итог проверки недоступен."))
            except Exception:
                log.exception("Не удалось отправить итог /check; он сохранён для /status")
                return
            if result is None or result.status == "retry":
                self.db.set_flag("check_summary_pending", "1")

    def run_cycle(self):
        with self._lock:
            if self._scan_running:
                return None
            self._scan_running = True
            requested = self.db.get_flag("check_pending") == "1"
            if requested:
                self.db.set_flag("check_in_progress", "1")
                self.db.set_flag("check_pending", "0")
        summary = "Проверка прервана ошибкой. Состояние сохранено; подробности — /status и логи."
        try:
            report = self._run_cycle()
            summary = (f"{'Проверка прервана' if self.stop_event.is_set() else 'Проверка завершена'}.\n"
                       f"Источники: {report['successful']}/{report['attempted']} успешно.\n"
                       f"Получено записей: {report['found']}.\n"
                       f"Новых подходящих: {report['matched']}. Добавлено в очередь: {report['new']}.\n"
                       f"Отправлено за цикл: {report['sent']}. "
                       f"Ожидают в очереди: {self.db.multi_delivery_counts().get('pending', 0)}.")
            if not self.is_enabled():
                summary += "\nРассылка выключена. Объявления сохранены без отправки."
            if report['errors']:
                summary += "\nОшибки источников:\n" + "\n".join(report['errors'])
            return report
        finally:
            with self._lock:
                if requested and not self.stop_event.is_set():
                    self.db.set_flag("check_summary", summary)
                    self.db.set_flag("check_summary_pending", "1")
                    self.db.set_flag("check_in_progress", "0")
                self._scan_running = False

    # ---------- сбор одного источника ----------
    def collect_source(self, key: str, name: str, fetch) -> list[Item] | None:
        try:
            items = fetch()
            self.db.set_source_state(key, error="")
            return items
        except Exception as e:  # noqa: BLE001 — ошибка источника не останавливает остальные
            log.error("источник %s: ошибка сбора: %s", key, e)
            self.db.set_source_state(key, error=str(e)[:300])
            return None

    # ---------- обработка найденного объявления ----------
    def process_item(self, item: Item, *, baseline: bool) -> bool:
        """Возвращает True, если объявление новое, подходящее и подлежит отправке."""
        if self.db.exists(item.item_key):
            return False
        ok, reason = self.filters.check(item)  # правила применимы и к baseline — мусор не храним
        if not ok:
            log.debug("skip %s: %s", item.item_key, reason)
            # сохраняем как отброшенное, чтобы не проверять каждый час повторно
            self.db.add_item(item, baseline=True, category=None)
            return False
        if self.db.is_duplicate(item):
            log.info("duplicate across sources: %s (%s)", item.item_key, item.title)
            self.db.add_item(item, baseline=True, category=item.categories[0])
            self.db.set_delivery(item.item_key, "skipped", "duplicate")
            return False
        self.db.add_item(item, baseline=baseline, category=item.categories[0],
                         eligible_at=(getattr(self, "_cycle_started", time.time()) if item.published_at
                                      else getattr(self, "_undated_cutoff", time.time())))
        return not baseline

    def format_message(self, item):
        return format_message(item, self.cfg.rules)

    def _item_from_row(self, row):
        return self.db.deserialize(row["payload"]) if row["payload"] else None

    def deliver(self, item, *, telegram_id=None, now=None):
        now = now or datetime.now(timezone.utc)
        telegram_id = self.cfg.owner_id if telegram_id is None else telegram_id
        with self._lock:
            self.fill_delivery_queue(now)
            row = self.db.user_delivery(telegram_id, item.item_key)
            user = self.db.user(telegram_id)
            if (not row or row['status'] != 'pending' or row['available_at'] > now.timestamp()
                    or not user or not user['is_active'] or self.stop_event.is_set()):
                return False
            if float(self.db.get_flag('send_after', '0')) > now.timestamp():
                return False
            accepted, reason = self.filters.check(item, now=now)
            if not accepted:
                self.db.finish_user_delivery(row['id'], 'expired', reason)
                return False
            self.db.refresh_pending(item)
            message = self.format_message(item)
            if not self.db.claim_user_delivery(row['id'], now):
                return False
            self.db.set_flag('send_after', str(max(now.timestamp(),time.time())+1.1))
            try:
                result = self.notifier.send(message, chat_id=telegram_id)
            except Exception:
                self.db.finish_user_delivery(row['id'], 'uncertain', 'unexpected_send_error')
                return False
            completed = max(now.timestamp(), time.time())
            # Throttle every HTTP attempt, including ambiguous and rejected requests.
            self.db.set_flag('send_after', str(completed+1.1))
            if result.status == 'sent':
                self.db.finish_user_delivery(row['id'], 'sent', 'delivered', message_id=result.message_id)
                return True
            if result.status == 'retry':
                attempts = row['attempts']+1
                delay = (result.retry_after if result.code == 429 else
                         max(result.retry_after, min(3600,5*2**min(attempts,10))))
                self.db.finish_user_delivery(row['id'], 'pending', 'telegram_retry', available_at=completed+delay)
                if result.code == 429:
                    self.db.set_flag('send_after', str(completed+delay))
            else:
                self.db.finish_user_delivery(row['id'], result.status, f'telegram_{result.code}')
            if result.code == 403:
                self.db.user_control(telegram_id, active=False)
            if result.code == 401:
                self.fatal_error = 'Telegram token rejected'
                self.stop_event.set()
            return False

    def delivery_sources(self):
        sources = {f"tg_{c.username.lower()}":c for c in self.cfg.channels}
        sources.update({w.key:w for w in self.cfg.websites
                        if w.type in WEBSITE_COLLECTORS and w.type != "rtkit_careers"})
        return sources

    def fill_delivery_queue(self, now):
        configured = self.delivery_sources()
        ready = (self._available_sources & configured.keys()
                 if now.timestamp()-self._last_scan <= 2*self.cfg.poll_interval_seconds else set())
        self.db.fill_user_queue(available_sources=ready, configured_sources=configured)

    def drain_pending(self, *, now=None):
        now = now or datetime.now(timezone.utc)
        sources = self.delivery_sources()
        sent = 0
        self.fill_delivery_queue(now)
        rows = self.db.user_pending(now, after_id=getattr(self, '_delivery_cursor', 0))
        if not rows:
            self._delivery_cursor = 0
            rows = self.db.user_pending(now)
        for row in rows:
            if self.stop_event.is_set() or not self.is_enabled():
                break
            if float(self.db.get_flag("send_after","0")) > now.timestamp():
                break
            self._delivery_cursor = row["delivery_id"]
            if row["source_key"] not in sources:
                self.db.finish_user_delivery(row["delivery_id"],"skipped","source_disabled_or_unverified")
                continue
            if row["source_key"] not in self._available_sources or now.timestamp()-self._last_scan > 2*self.cfg.poll_interval_seconds:
                continue
            current=self._fresh_items.get(row["item_key"])
            source=sources[row["source_key"]]
            if current is None and source.type=="telegram_channel":
                try:
                    current=telegram_channel.fetch_post(source.username,int(row["external_id"]))
                except Exception:
                    self.db.set_source_state(source.key,error="Не удалось повторно прочитать публикацию Telegram")
                    self._available_sources.discard(row["source_key"])
                    continue
                if current:
                    self._fresh_items[current.item_key]=current
            if current is None:
                self.db.finish_user_delivery(row["delivery_id"],"expired","publication_no_longer_present")
                continue
            if self.deliver(current,telegram_id=row["telegram_id"],now=now):
                sent+=1
            if self.stop_event.is_set():
                break
        return sent

    def _run_cycle(self):
        started = datetime.now(timezone.utc)
        found = new_count = notified = 0
        errors: list[str] = []
        checked_keys = []
        successful = matched = 0
        self._fresh_items = {}
        self._available_sources = set()
        self._last_scan = time.time()
        self._cycle_started = started.timestamp()

        def handle_source(key: str, items: list[Item], *, initial_snapshot: bool = False) -> tuple[int, int]:
            nonlocal matched
            first = not self.db.source_initialized(key)
            # Undated website publications need a successful personal baseline:
            # the first snapshot after subscription/resume cannot establish recency.
            self._undated_cutoff = float(self.db.get_flag('source_snapshot:'+key, '0'))
            new_items=0
            for it in items:
                self._fresh_items[it.item_key]=it
                with self._lock:
                    already_seen = self.db.exists(it.item_key)
                    baseline = initial_snapshot
                    if self.process_item(it,baseline=baseline):
                        new_items+=1
                    if not already_seen and not initial_snapshot:
                        row = self.db.item_row(it.item_key)
                        if row and row['delivery_state'] in {'pending', 'baseline'}:
                            matched += 1
            if first:
                self.db.set_source_state(key,initialized=True)
            self.db.set_flag("source_snapshot:"+key, str(self._cycle_started))
            return len(items),new_items

        # Telegram-каналы: только открытые веб-страницы t.me/s/<username>
        for ch in self.cfg.channels:
            if self.stop_event.is_set():
                break
            checked_keys.append(ch.key)
            source_key = f"tg_{ch.username.lower()}"  # Existing seen_items keys remain unchanged.
            try:
                cursor = self.db.telegram_cursor(source_key)
                # Persist the legacy boundary before processing, so a crash partway
                # through ingestion cannot turn a partial batch into a new cursor.
                self.db.save_telegram_cursor(source_key, cursor)
                items = telegram_channel.fetch_channel(ch.username, since_id=cursor, max_pages=ch.max_pages)
                n_found, n_new = handle_source(ch.key, items, initial_snapshot=cursor is None)
                if items:
                    self.db.save_telegram_cursor(source_key, max(int(it.external_id) for it in items))
                self.db.set_source_state(ch.key, error="")
                self._available_sources.add(source_key)
                successful += 1
                found += n_found
                new_count += n_new
            except Exception as exc:
                log.error("источник %s: ошибка Telegram: %s", ch.key, exc)
                self.db.set_source_state(ch.key, error=str(exc)[:300])

        # Сайты
        for w in self.cfg.websites:
            if self.stop_event.is_set():
                break
            checked_keys.append(w.key)
            cls = WEBSITE_COLLECTORS.get(w.type)
            if cls is None:
                errors.append(f"{w.key}: нет сборщика для типа {w.type}")
                continue
            collector = cls(w)
            items = self.collect_source(w.key, w.name, collector.fetch)
            if items is None:
                continue
            n_found, n_new = handle_source(w.key, items, initial_snapshot=not self.db.source_initialized(w.key))
            self._available_sources.add(w.key)
            successful += 1
            found += n_found
            new_count += n_new

        notified = self.drain_pending()

        # состояние ошибок источников
        for key in checked_keys:
            err = self.db.get_source_error(key)
            if err:
                errors.append(f"{key}: {err}")

        self.db.log_run(started.isoformat(), datetime.now(timezone.utc).isoformat(),
                        found, new_count, notified, "; ".join(errors)[:500])
        log.info("цикл завершён: собрано %d, новых подходящих %d, отправлено %d, ошибок %d",
                 found, new_count, notified, len(errors))
        return {'attempted': len(checked_keys), 'successful': successful, 'found': found,
                'matched': matched, 'new': new_count, 'sent': notified, 'errors': errors}

    def loop(self, stop):
        next_scan=0.0
        while not stop.is_set():
            try:
                self.flush_command_reply()
                if self.scan_requested.is_set():
                    self.scan_requested.clear()
                    next_scan=0.0
                if time.monotonic() >= next_scan:
                    self.run_cycle()
                    next_scan=time.monotonic()+self.cfg.poll_interval_seconds
                else:
                    self.drain_pending()
                self.flush_check_summary()
            except Exception:
                log.exception("Ошибка цикла; состояние очереди сохранено")
                next_scan=time.monotonic()+self.cfg.poll_interval_seconds
            stop.wait(2)


HELP = ("/start — включить уведомления\n/status — состояние подписки\n"
        "/stop — отключить уведомления\n/help — помощь")
WELCOME = ("Привет! 👋\n\nЯ IT Radar — бот, который помогает находить стажировки, "
           "хакатоны, конференции, конкурсы и другие возможности в IT.\n\n"
           "🔎 Я отслеживаю публикации из Telegram-каналов и карьерных сайтов.\n"
           "🔔 Уведомления включены! Архив не рассылается.\n\n" + HELP)


def handle_update(bot, upd):
    identifier = upd.get('update_id')
    if type(identifier) is not int or identifier < 0:
        return
    with bot._lock:
        if identifier < int(bot.db.get_flag('offset', '0')):
            return
        msg = upd.get('message') or {}
        chat, sender = msg.get('chat', {}), msg.get('from', {})
        telegram_id = sender.get('id')
        authorized = (type(telegram_id) is int and telegram_id > 0
                      and chat.get('type') == 'private' and chat.get('id') == telegram_id
                      and not sender.get('is_bot', False))
        text = msg.get('text')
        command = text.split(maxsplit=1)[0] if isinstance(text,str) and text.strip() else ''
        if '@' in command:
            command, username = command.split('@',1)
            authorized = authorized and username.lower() == bot.bot_username
        active = None
        reply = None
        if authorized:
            if command == '/start':
                active = True
                reply = WELCOME
            elif command == '/stop':
                active = False
                reply = 'Уведомления отключены. /start — возобновить без рассылки архива.'
            elif command == '/status':
                user = bot.db.user(telegram_id)
                counts = bot.db.user_delivery_counts(telegram_id)
                reply = (f"Уведомления: {'включены' if user and user['is_active'] else 'выключены'}.\n"
                         f"Ожидают отправки: {counts.get('pending',0)}.\n"
                         f"Приостановлены из-за источников: {counts.get('suspended',0)}.\n"
                         f"Неизвестный результат: {counts.get('uncertain',0)}.\n" + HELP)
            elif command == '/help':
                reply = 'IT Radar присылает новые подходящие IT-возможности.\n' + HELP
            elif command == '/check' and telegram_id == bot.cfg.owner_id:
                accepted, reply = bot.check_request_reply(time.time())
                if accepted:
                    bot.db.apply_control(check_requested_at=time.time())
                    bot.scan_requested.set()
            else:
                reply = 'Неизвестная команда.\n' + HELP
        bot.db.user_control(telegram_id if authorized else 0,
                            sender.get('username') if isinstance(sender.get('username'),str) else None,
                            active=active, offset=identifier+1, reply=reply)
        if reply:
            bot.flush_command_reply()


def poll_updates(bot, stop):
    while not stop.is_set():
        try:
            updates=bot.notifier.call("getUpdates",{"offset":int(bot.db.get_flag("offset","0")),
                                      "timeout":25,"allowed_updates":["message"]},timeout=35)
            if not isinstance(updates,list):
                raise BotAPIError()
            for update in updates:
                if stop.is_set():
                    return
                if isinstance(update,dict):
                    handle_update(bot,update)
        except BotAPIError as exc:
            if exc.code in {401,409}:
                bot.fatal_error=f"Telegram polling failed ({exc.code}); check token and duplicate processes"
                stop.set()
                return
            log.warning("Опрос команд недоступен: код %s",exc.code)
            stop.wait(max(5,exc.retry_after))
        except Exception:
            log.exception("Ошибка обработки команд")
            stop.wait(5)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-config", action="store_true", help="Проверить настройки без сети и запуска бота")
    parser.add_argument("--delivery-status", action="store_true", help="Состояния очереди в остановленной БД, без сети")
    parser.add_argument("--telegram-id", type=int, help="Подписчик для диагностики или ручного разбора доставки")
    parser.add_argument("--resolve-item", help="Ключ записи очереди для ручного разбора")
    parser.add_argument("--resolution", choices=["sent","retry","skip"])
    parser.add_argument("--allow-duplicate-risk", action="store_true", help="Подтвердить риск дубля при ручном повторе неизвестной доставки")
    args = parser.parse_args()
    if bool(args.resolve_item) != bool(args.resolution):
        parser.error("--resolve-item и --resolution задаются вместе")
    if sum(bool(x) for x in (args.check_config,args.delivery_status,args.resolve_item)) > 1:
        parser.error("Выберите один режим диагностики")
    configure_logging()
    try:
        cfg = Config.load()
        if not args.delivery_status and not args.resolve_item:
            cfg.validate_runtime()
    except ConfigurationError as exc:
        log.error("Ошибка конфигурации: %s", exc)
        raise SystemExit(2) from None
    except Exception as exc:
        # Do not let Python's unformatted traceback bypass credential redaction.
        log.error("Ошибка конфигурации (%s). Проверьте окружение и CONFIG_PATH.", type(exc).__name__)
        raise SystemExit(2) from None
    if args.check_config:
        log.info("Конфигурация корректна; сеть и база данных не использовались.")
        return
    if args.delivery_status or args.resolve_item:
        if not Path(cfg.db_path).is_file():
            raise RuntimeError("Database does not exist; set DB_PATH")
        db=Database(cfg.db_path)
        try:
            if args.resolve_item:
                if args.telegram_id is not None:
                    db.resolve_user_delivery(args.telegram_id,args.resolve_item,args.resolution,args.allow_duplicate_risk)
                else:
                    # Legacy diagnostics are retained only for rows without personal tasks.
                    if db.has_personal_delivery(args.resolve_item):
                        raise RuntimeError("Specify --telegram-id for a personal delivery")
                    row=db.item_row(args.resolve_item)
                    if row and row['delivery_state'] in {'uncertain','legacy_review'} and args.resolution=='retry' and not args.allow_duplicate_risk:
                        raise RuntimeError("Check Telegram first; retry requires --allow-duplicate-risk")
                    db.resolve_delivery(args.resolve_item,args.resolution)
                log.info("Решение оператора сохранено; сообщения не отправлялись.")
            log.info("Очередь: %s",json.dumps(db.multi_delivery_counts(args.telegram_id),ensure_ascii=False))
            for row in db.multi_review(args.telegram_id):
                log.info("%s",json.dumps(dict(row),ensure_ascii=False))
        finally:
            db.close()
        return
    bot = Bot(cfg)
    stop = bot.stop_event
    signal.signal(signal.SIGTERM, lambda *a: stop.set())
    signal.signal(signal.SIGINT, lambda *a: stop.set())

    try:
        bot.initialize()
    except Exception:
        bot.db.close()
        raise
    poller = threading.Thread(target=poll_updates, args=(bot, stop), daemon=True, name="poller")
    poller.start()
    log.info("it-radar-bot запущен: %d каналов, %d сайтов, интервал %d c",
             len(cfg.channels), len(cfg.websites), cfg.poll_interval_seconds)
    try:
        bot.loop(stop)
    finally:
        log.info("остановка…")
        stop.set()
        poller.join(timeout=50)
        bot.db.close()
    if bot.fatal_error:
        raise RuntimeError(bot.fatal_error)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log.exception("Не удалось запустить или продолжить работу бота")
        raise SystemExit(1) from None
