"""it-radar-bot: Telegram-бот, который следит за возможностями для студентов и
начинающих IT-специалистов в заданных источниках (Telegram-каналы через t.me/s/ и
сайты компаний) и присылает владельцу только новые подходящие объявления.

Запуск:  python -m app.main          (токен и владелец — из переменных окружения)
Команды: /start — включить рассылку, /stop — выключить, /status — состояние источников.
"""
from __future__ import annotations

import argparse
import html
import logging
import signal
import threading
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app.collectors import croc, k2, rtkit, telegram_channel
from app.config import Config, ConfigurationError
from app.db import Database
from app.filters import Filters
from app.models import Item
from app.notify import TelegramNotifier
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
        self._lock = threading.Lock()

    # ---------- флаг включения ----------
    def is_enabled(self) -> bool:
        return self.db.get_flag("enabled") == "1"

    def ever_started(self) -> bool:
        """Владелец хоть раз включал рассылку (/start). До этого — исходная точка."""
        return self.db.get_flag("ever_started") == "1"

    # совместимость
    is_enabled_ever = ever_started

    def set_enabled(self, on: bool) -> None:
        self.db.set_flag("enabled", "1" if on else "0")
        if on:
            self.db.set_flag("ever_started", "1")

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
        if self.db.title_hash_seen(item.title_hash):
            log.info("duplicate across sources: %s (%s)", item.item_key, item.title)
            self.db.add_item(item, baseline=True, category=item.categories[0])
            return False
        self.db.add_item(item, baseline=baseline, category=item.categories[0])
        return not baseline

    def deliver(self, item: Item) -> bool:
        """Отправка одного объявления владельцу + отметка о доставке."""
        if self.notifier.send(self.format_message(item)):
            self.db.mark_notified(item.item_key)
            return True
        return False

    def format_message(self, item: Item) -> str:
        e = TelegramNotifier.escape
        labels = [self.cfg.rules.categories[c]["label"] for c in item.categories if c in self.cfg.rules.categories]
        lines = [
            f"📡 <b>Новая возможность</b>",
            f"Категория: <b>{e(', '.join(labels) or '—')}</b>",
            f"Источник: <b>{e(item.source_name)}</b>",
            f"<b>{e(item.title)}</b>",
        ]
        summary = (item.summary or "").strip()
        if summary:
            lines.append(e(summary[:350]) + ("…" if len(summary) > 350 else ""))
        # статус регистрации: «открыта» утверждаем только при явных признаках
        if item.registration_status == "open":
            status = "набор/регистрация открыты (по тексту объявления)"
        elif item.registration_status == "closed":
            status = "закрыто"
        else:
            status = "статус регистрации неизвестен — проверьте на странице"
        lines.append(f"Статус: {e(status)}")
        if item.deadline:
            tz = ZoneInfo(self.cfg.rules.timezone)
            deadline = item.deadline.astimezone(tz) if item.deadline.tzinfo else item.deadline.replace(tzinfo=tz)
            lines.append(f"Срок: {deadline:%Y-%m-%d %H:%M} ({e(self.cfg.rules.timezone)})")
        lines.append(f'Оригинал: <a href="{item.url}">{e(item.url)}</a>')
        if item.apply_url:
            lines.append(f'Заявка/регистрация: <a href="{item.apply_url}">{e(item.apply_url)}</a>')
        return "\n".join(lines)

    # ---------- один цикл проверки всех источников ----------
    def _item_from_row(self, row) -> Item:
        """Восстановление Item из строки БД (для ретрайда неотправленного)."""
        item = Item(
            source_key=row["source_key"], source_name=row["source_key"],
            external_id=row["external_id"], url=row["url"] or "",
            title=row["title"] or "", summary=row["summary"] or "",
            apply_url=row["apply_url"], published_at=None,
            categories=[row["category"]] if row["category"] else [],
        )
        ch = next((c for c in self.cfg.channels if f"tg_{c.username.lower()}" == row["source_key"]), None)
        wsrc = next((w.name for w in self.cfg.websites if w.key == row["source_key"]), None)
        item.source_name = wsrc or (ch.display_name if ch else row["source_key"])
        return item

    def run_cycle(self) -> None:
        started = datetime.now(timezone.utc)
        enabled = self.is_enabled()
        found = new_count = notified = 0
        errors: list[str] = []

        # сначала ретрайд неотправленного (после рестарта/ошибок сети)
        unverified_sources = {w.key for w in self.cfg.websites if w.type == "rtkit_careers"}
        if enabled:
            for row in self.db.pending_unnotified():
                # Old RTKit collectors stored disabled demo fixtures. Preserve
                # those rows, but never deliver them as real opportunities.
                if row["source_key"] in unverified_sources:
                    continue
                item = self._item_from_row(row)
                if self.deliver(item):
                    notified += 1
                else:
                    break  # канал доставки недоступен — попробуем в следующем цикле

        def handle_source(key: str, items: list[Item], *, initial_snapshot: bool = False) -> tuple[int, int]:
            nonlocal notified
            first = not self.db.source_initialized(key)
            # При выключенной рассылке (/stop) новые объявления только запоминаем,
            # чтобы после /start не прислать накопившийся архив.
            baseline = True if initial_snapshot or not enabled else (first and not self.is_enabled_ever())
            to_send: list[Item] = []
            for it in items:
                if self.process_item(it, baseline=baseline):
                    to_send.append(it)
            if first:
                self.db.set_source_state(key, initialized=True)
                log.info("источник %s: первый запуск — запомнено %d публикаций как исходная точка", key, len(items))
            if enabled:
                for it in to_send:
                    if self.deliver(it):
                        notified += 1
                    else:
                        break  # доставка недоступна — остальное уйдёт через pending_unnotified
            return len(items), len(to_send)

        # Telegram-каналы: только открытые веб-страницы t.me/s/<username>
        for ch in self.cfg.channels:
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
                found += n_found
                new_count += n_new
            except Exception as exc:
                log.error("источник %s: ошибка Telegram: %s", ch.key, exc)
                self.db.set_source_state(ch.key, error=str(exc)[:300])

        # Сайты
        for w in self.cfg.websites:
            cls = WEBSITE_COLLECTORS.get(w.type)
            if cls is None:
                errors.append(f"{w.key}: нет сборщика для типа {w.type}")
                continue
            collector = cls(w)
            items = self.collect_source(w.key, w.name, collector.fetch)
            if items is None:
                continue
            n_found, n_new = handle_source(w.key, items, initial_snapshot=not self.db.source_initialized(w.key))
            found += n_found
            new_count += n_new

        # состояние ошибок источников
        for key in [c.key for c in self.cfg.channels] + [w.key for w in self.cfg.websites]:
            err = self.db.get_source_error(key)
            if err:
                errors.append(f"{key}: {err}")

        self.db.log_run(started.isoformat(), datetime.now(timezone.utc).isoformat(),
                        found, new_count, notified, "; ".join(errors)[:500])
        log.info("цикл завершён: собрано %d, новых подходящих %d, отправлено %d, ошибок %d",
                 found, new_count, notified, len(errors))

    def loop(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.run_cycle()
            except Exception:  # noqa: BLE001 — цикл не должен умирать
                log.exception("цикл завершился с неожиданной ошибкой")
            stop.wait(self.cfg.poll_interval_seconds)


# ---------- Telegram Bot API: long polling getUpdates ----------

def handle_update(bot: Bot, upd: dict) -> None:
    msg = upd.get("message") or upd.get("edited_message")
    if not msg:
        return
    chat_id = msg["chat"]["id"]
    text = (msg.get("text") or "").strip().lower()
    if chat_id != bot.cfg.owner_id:
        # уведомления — только владельцу; чужие сообщения игнорируем
        log.info("команда от не-владельца %s — проигнорировано", chat_id)
        return
    reply = None
    if text.startswith("/start"):
        bot.set_enabled(True)
        reply = ("✅ Рассылка включена. Источники проверяются примерно раз в час, "
                 "присылаю только новые подходящие объявления.")
    elif text.startswith("/stop"):
        bot.set_enabled(False)
        reply = "⏸ Рассылка выключена. Команда /start включит обратно."
    elif text.startswith("/status"):
        rows = bot.db.source_states()
        lines = [f"Рассылка: {'включена' if bot.is_enabled() else 'выключена'}",
                 f"Сохранено публикаций: {bot.db.count_items()}", "Источники:"]
        for r in rows:
            st = f"OK" if not r["last_error"] else f"ОШИБКА: {r['last_error']}"
            lines.append(f"• {r['source_key']}: {st} (проверка {r['last_check_at'] or '—'})")
        reply = "\n".join(lines)
    elif text.startswith("/help"):
        reply = "/start — включить, /stop — выключить, /status — состояние источников."
    if reply:
        payload = {"chat_id": chat_id, "text": reply}
        import requests
        requests.post(f"https://api.telegram.org/bot{bot.cfg.bot_token}/sendMessage",
                      json=payload, timeout=15)


def poll_updates(bot: Bot, stop: threading.Event) -> None:
    offset = int(bot.db.get_flag("offset") or 0)
    api = f"https://api.telegram.org/bot{bot.cfg.bot_token}/getUpdates"
    while not stop.is_set():
        try:
            import requests
            r = requests.get(api, params={"offset": offset, "timeout": 25}, timeout=40)
            data = r.json()
            if data.get("ok"):
                for upd in data["result"]:
                    offset = max(offset, upd["update_id"] + 1)
                    try:
                        handle_update(bot, upd)
                    except Exception:  # noqa: BLE001
                        log.exception("обработка апдейта %s", upd.get("update_id"))
                bot.db.set_flag("offset", str(offset))
            else:
                log.warning("getUpdates: %s", data.get("description"))
                stop.wait(10)
        except Exception as e:  # noqa: BLE001
            log.warning("getUpdates недоступен: %s", e)
            stop.wait(10)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-config", action="store_true", help="Проверить настройки без сети и запуска бота")
    args = parser.parse_args()
    configure_logging()
    try:
        cfg = Config.load()
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
    bot = Bot(cfg)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *a: stop.set())
    signal.signal(signal.SIGINT, lambda *a: stop.set())

    poller = threading.Thread(target=poll_updates, args=(bot, stop), daemon=True, name="poller")
    poller.start()
    log.info("it-radar-bot запущен: %d каналов, %d сайтов, интервал %d c",
             len(cfg.channels), len(cfg.websites), cfg.poll_interval_seconds)
    try:
        bot.loop(stop)
    finally:
        log.info("остановка…")
        stop.set()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log.exception("Не удалось запустить или продолжить работу бота")
        raise SystemExit(1) from None
