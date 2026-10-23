"""it-radar-bot: Telegram-бот, который следит за возможностями для студентов и
начинающих IT-специалистов в заданных источниках (Telegram-каналы через t.me/s/ и
сайты компаний) и присылает владельцу только новые подходящие объявления.

Запуск:  python -m app.main          (токен и владелец — из переменных окружения)
Команды: /start — включить рассылку, /stop — выключить, /status — состояние источников.
"""
from __future__ import annotations

import argparse
import json
import logging
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
        self.fatal_error = None
        self.bot_username = ""
        self._fresh_items = {}
        self._available_sources = set()
        self._last_scan = 0.0
        if cfg.owner_id > 0:
            try:
                self.db.bind_identity("owner_id", cfg.owner_id)
            except Exception:
                self.db.close()
                raise

    # ---------- флаг включения ----------
    def is_enabled(self) -> bool:
        return self.db.get_flag("enabled") == "1"

    def ever_started(self) -> bool:
        """Владелец хоть раз включал рассылку (/start). До этого — исходная точка."""
        return self.db.get_flag("ever_started") == "1"

    # совместимость
    is_enabled_ever = ever_started

    def set_enabled(self, on: bool) -> None:
        with self._lock:
            was_enabled = self.is_enabled()
            self.db.apply_control(enabled=on)
            if on and not was_enabled:
                self.scan_requested.set()

    def initialize(self):
        me = self.notifier.call("getMe")
        info = self.notifier.call("getWebhookInfo")
        if not isinstance(me, dict) or not isinstance(me.get("username"), str) or type(me.get("id")) is not int:
            raise RuntimeError("Unexpected getMe response")
        if not isinstance(info, dict) or info.get("url"):
            raise RuntimeError("Webhook configured or invalid response; remove webhook explicitly before long polling")
        self.db.bind_identity("bot_id", me["id"])
        self.bot_username = me["username"].lower()

    def service_message(self, text):
        with self._lock:
            if self.stop_event.is_set():
                return
            delay = float(self.db.get_flag("send_after", "0")) - time.time()
            if delay > 1.1:
                return  # A server-imposed rate limit: do not block command handling.
            if delay > 0 and self.stop_event.wait(delay):
                return
            result = self.notifier.send(escaped_clip(text, 3800))
            if result.status == "sent":
                self.db.set_flag("send_after",str(time.time()+1.1))
            if result.status == "retry" and result.code == 429:
                self.db.set_flag("send_after",str(time.time()+result.retry_after))
            if result.code == 403:
                self.db.apply_control(enabled=False)
            if result.code == 401:
                self.fatal_error = "Telegram token rejected"
                self.stop_event.set()

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
        self.db.add_item(item, baseline=baseline, category=item.categories[0])
        return not baseline

    def format_message(self, item):
        return format_message(item, self.cfg.rules)

    def _item_from_row(self, row):
        return self.db.deserialize(row["payload"]) if row["payload"] else None

    def deliver(self, item, *, now=None):
        now = now or datetime.now(timezone.utc)
        with self._lock:
            row = self.db.item_row(item.item_key)
            if not row or row["delivery_state"] != "pending" or row["available_at"] > now.timestamp():
                return False
            if not self.is_enabled() or self.stop_event.is_set():
                return False
            if float(self.db.get_flag("send_after", "0")) > now.timestamp():
                return False
            accepted, reason = self.filters.check(item, now=now)
            if not accepted:
                self.db.set_delivery(item.item_key, "expired", reason)
                return False
            self.db.refresh_pending(item)
            message = self.format_message(item)
            if not self.db.claim_delivery(item.item_key, now):
                return False
            # Any unexpected failure after the claim leaves 'sending', which is
            # quarantined as uncertain on restart. Never blindly replay it.
            try:
                result = self.notifier.send(message)
            except Exception:
                self.db.set_delivery(item.item_key,"uncertain","unexpected_send_error")
                return False
            completed = max(now.timestamp(), time.time())
            if result.status == "sent":
                self.db.set_delivery(item.item_key,"sent","delivered",message_id=result.message_id)
                self.db.set_flag("send_after",str(completed+1.1))
                return True
            if result.status == "retry":
                row=self.db.item_row(item.item_key)
                delay = (result.retry_after if result.code == 429 else
                         max(result.retry_after, min(3600, 5 * 2 ** min(row["attempts"],10))))
                available=completed+delay
                self.db.set_delivery(item.item_key,"pending","telegram_retry",available_at=available)
                self.db.set_flag("send_after",str(available))
            else:
                self.db.set_delivery(item.item_key,result.status,f"telegram_{result.code}")
            if result.code == 403:
                self.db.apply_control(enabled=False)
            if result.code == 401:
                self.fatal_error="Telegram token rejected"
                self.stop_event.set()
            return False

    def drain_pending(self, *, now=None):
        now=now or datetime.now(timezone.utc)
        sources={f"tg_{c.username.lower()}":c for c in self.cfg.channels}
        sources.update({w.key:w for w in self.cfg.websites if w.type in WEBSITE_COLLECTORS and w.type!="rtkit_careers"})
        sent=0
        for row in self.db.pending_unnotified(now):
            if self.stop_event.is_set() or not self.is_enabled():
                break
            if row["source_key"] not in sources:
                self.db.set_delivery(row["item_key"],"skipped","source_disabled_or_unverified")
                continue
            if row["source_key"] not in self._available_sources or now.timestamp()-self._last_scan > 2*self.cfg.poll_interval_seconds:
                continue
            if float(self.db.get_flag("send_after","0")) > now.timestamp():
                break
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
                self.db.set_delivery(row["item_key"],"expired","publication_no_longer_present")
                continue
            if self.deliver(current,now=now):
                sent+=1
            state=self.db.item_row(row["item_key"])["delivery_state"]
            if state in {"pending","uncertain"} or self.stop_event.is_set():
                break
        return sent

    def run_cycle(self) -> None:
        started = datetime.now(timezone.utc)
        found = new_count = notified = 0
        errors: list[str] = []
        self._fresh_items = {}
        self._available_sources = set()
        self._last_scan = time.time()
        scan_enabled = self.is_enabled()
        scan_generation = self.db.get_flag("resume_generation", "0")

        def handle_source(key: str, items: list[Item], *, initial_snapshot: bool = False) -> tuple[int, int]:
            first = not self.db.source_initialized(key)
            new_items=0
            for it in items:
                self._fresh_items[it.item_key]=it
                with self._lock:
                    generation = self.db.get_flag("resume_generation", "0")
                    resume_snapshot = self.db.get_flag("source_generation:"+key,"0") != generation
                    baseline = (initial_snapshot or not scan_enabled or not self.is_enabled()
                                or generation != scan_generation or resume_snapshot)
                    if self.process_item(it,baseline=baseline):
                        new_items+=1
            if first:
                self.db.set_source_state(key,initialized=True)
            self.db.set_flag("source_generation:"+key,scan_generation)
            return len(items),new_items

        # Telegram-каналы: только открытые веб-страницы t.me/s/<username>
        for ch in self.cfg.channels:
            if self.stop_event.is_set():
                break
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
                found += n_found
                new_count += n_new
            except Exception as exc:
                log.error("источник %s: ошибка Telegram: %s", ch.key, exc)
                self.db.set_source_state(ch.key, error=str(exc)[:300])

        # Сайты
        for w in self.cfg.websites:
            if self.stop_event.is_set():
                break
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
            found += n_found
            new_count += n_new

        notified = self.drain_pending()

        # состояние ошибок источников
        for key in [c.key for c in self.cfg.channels] + [w.key for w in self.cfg.websites]:
            err = self.db.get_source_error(key)
            if err:
                errors.append(f"{key}: {err}")

        self.db.log_run(started.isoformat(), datetime.now(timezone.utc).isoformat(),
                        found, new_count, notified, "; ".join(errors)[:500])
        log.info("цикл завершён: собрано %d, новых подходящих %d, отправлено %d, ошибок %d",
                 found, new_count, notified, len(errors))

    def loop(self, stop):
        next_scan=0.0
        while not stop.is_set():
            try:
                if self.scan_requested.is_set():
                    self.scan_requested.clear()
                    next_scan=0.0
                if time.monotonic() >= next_scan:
                    self.run_cycle()
                    next_scan=time.monotonic()+self.cfg.poll_interval_seconds
                else:
                    self.drain_pending()
            except Exception:
                log.exception("Ошибка цикла; состояние очереди сохранено")
                next_scan=time.monotonic()+self.cfg.poll_interval_seconds
            stop.wait(2)


def handle_update(bot, upd):
    identifier=upd.get("update_id")
    if type(identifier) is not int or identifier<0:
        return
    with bot._lock:
        if identifier<int(bot.db.get_flag("offset","0")):
            return
        msg=upd.get("message") or {}
        chat,sender=msg.get("chat",{}),msg.get("from",{})
        authorized=(chat.get("type")=="private" and chat.get("id")==bot.cfg.owner_id
                    and sender.get("id")==bot.cfg.owner_id)
        command=(msg.get("text") or "").split(maxsplit=1)[0] if msg.get("text") else ""
        if "@" in command:
            command,username=command.split("@",1)
            authorized=authorized and username.lower()==bot.bot_username
        enabled=None
        reply=None
        if authorized and command=="/start":
            enabled=True
            reply="Уведомления включены. Первый сбор и первая проверка после паузы сохраняются без архива. /stop — остановить, /status — состояние."
        elif authorized and command=="/stop":
            enabled=False
            reply="Уведомления остановлены. Ожидавшие отправки записи пропущены; архив за паузу не накапливается."
        elif authorized and command=="/status":
            counts=bot.db.delivery_counts()
            reply=(f"Уведомления: {'включены' if bot.is_enabled() else 'выключены'}. "
                   f"Ожидают: {counts.get('pending',0)}. Неизвестный результат: {counts.get('uncertain',0)}. "
                   f"Ошибки отправки: {counts.get('failed',0)}. Старая очередь для разбора: {counts.get('legacy_review',0)}.\n")
            reply+='\n'.join(f"{r['source_key']}: {r['last_error'] or 'OK'}" for r in bot.db.source_states())
        elif authorized and command=="/help":
            reply="/start — включить, /stop — выключить, /status — состояние. Неизвестные доставки разбираются оператором без автоматических повторов."
        was_enabled=bot.is_enabled()
        bot.db.apply_control(enabled=enabled,offset=identifier+1)
        if enabled is True and not was_enabled:
            bot.scan_requested.set()
        if reply:
            bot.service_message(reply)


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
                row=db.item_row(args.resolve_item)
                if row and row['delivery_state'] in {'uncertain','legacy_review'} and args.resolution=='retry' and not args.allow_duplicate_risk:
                    raise RuntimeError("Check Telegram first; retry requires --allow-duplicate-risk")
                db.resolve_delivery(args.resolve_item,args.resolution)
                log.info("Решение оператора сохранено; сообщения не отправлялись.")
            log.info("Очередь: %s",json.dumps(db.delivery_counts(),ensure_ascii=False))
            for row in db.review_deliveries():
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
