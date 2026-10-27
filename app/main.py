"""it-radar-bot: Telegram-бот, который следит за возможностями для студентов и
начинающих IT-специалистов в заданных источниках (Telegram-каналы через t.me/s/ и
сайты компаний) и присылает подписчикам новые подходящие объявления.

Запуск:  python -m app.main          (токен — из переменных окружения)
Команды: /app, /start, /stop, /status, /settings, /saved, /week, /reminders, /help.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import queue
from contextlib import contextmanager
import signal
import threading
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

from app.collectors import croc, k2, rtkit, telegram_channel
from app.collectors.website_http import WebsiteSnapshot
from app.collectors.base import collection_scope, check_collection, CollectionCancelled, CollectionTimeout
from app.config import Config, ConfigurationError
from app.db import Database
from app.backup import create_backup, BackupCancelled
from app.healthcheck import health_path, write_health, free_disk
from app.filters import Filters
from app.models import Item
from app.preferences import matches
from app.keyboards import save_keyboard
from app.miniapp import launch_keyboard
from app.agenda import render
from app.callbacks import handle_callback, consume_input
from app.reminders import format_reminder
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
        self.db = Database(cfg.db_path, metadata_timezone=cfg.rules.timezone)
        self.notifier = TelegramNotifier(cfg.bot_token, cfg.owner_id)
        self.filters = Filters(cfg.rules)
        # Флаг рассылки: /start включает, /stop выключает. Сохраняется в БД — переживает рестарт.
        self._lock = threading.RLock()
        self._send_lock = threading.RLock()  # HTTP slots serialize independently of command state
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
        self._unverified_items = set()
        self._fresh_items = {}
        self._available_sources = set()
        self._last_scan = 0.0
        self._background_collection = False
        self._sent_total = 0
        self._verified_at = {}
        self._missing_items = set()
        self._verify_after = {}
        self._cache_generation = 0
        self._verification_queue = queue.Queue(maxsize=128)
        self._verification_jobs = set()
        self._verification_retries = {}
        self._website_verified_at = {}
        self._website_failed = {}
        self._source_verify_after = {}
        self._source_locks = {}
        self.backup_error = None
        self._health_ready = False
        self._started_at = time.time()
        self._dispatcher_at = self._started_at
        self._health_write_at = 0.0
        self._poll_expected = False
        self._poll_at = self._started_at
        self._poll_retry_until = 0.0
        self._source_deadlines = {}
        self._last_report = None
        self._scan_error = False
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
        try:
            self.notifier.call('setMyCommands', {'commands':[
                {'command':command,'description':description} for command,description in [
                    ('start','Включить уведомления'),('stop','Выключить уведомления и напоминания'),
                    ('status','Статус подписки'),('settings','Персональные фильтры'),
                    ('saved','Избранные мероприятия'),('week','Афиша на семь дней'),
                    ('reminders','Управление напоминаниями'),('app','Открыть IT Radar'),('help','Справка')]]})
        except BotAPIError:
            log.warning('Не удалось обновить меню команд Telegram',exc_info=True)

        if self.cfg.miniapp_url and self.cfg.miniapp_menu_button:
            try:
                # Startup metadata call: no SQLite/bot lock is held. No polling process is started.
                self.notifier.call('setChatMenuButton', {'menu_button': {
                    'type': 'web_app', 'text': '📱 Открыть IT Radar',
                    'web_app': {'url': self.cfg.miniapp_url}}})
            except BotAPIError:
                log.warning('Не удалось настроить кнопку меню Mini App')

    def _transmit(self, message, telegram_id, *, version, timestamp, reply_markup=None):
        """Caller owns _send_lock, never _lock, while Telegram is in flight."""
        try:
            result = self.notifier.send(message,chat_id=telegram_id, **({'reply_markup':reply_markup} if reply_markup else {}))
        except Exception:
            with self._lock:
                self.db.set_flag('send_after',str(max(float(self.db.get_flag('send_after','0')),
                                                     max(timestamp,time.time())+1.1)))
            raise
        completed = max(timestamp,time.time())
        with self._lock:
            delay = max(1.1,result.retry_after) if result.status == 'retry' and result.code == 429 else 1.1
            self.db.set_flag('send_after',str(max(float(self.db.get_flag('send_after','0')),completed+delay)))
            if result.code == 403 and version is not None:
                self.db.deactivate_if_version(telegram_id,version)
            if result.code == 401:
                self.fatal_error = 'Telegram token rejected'
                self.stop_event.set()
        return result,completed

    def service_message(self, text, telegram_id=None, *, reply_markup=None, formatted=False):
        telegram_id = self.cfg.owner_id if telegram_id is None else telegram_id
        if telegram_id <= 0:
            return
        with self._send_lock:
            with self._lock:
                if self.stop_event.is_set() or float(self.db.get_flag('send_after','0')) > time.time():
                    return
                user = self.db.user(telegram_id)
                version = user['subscription_version'] if user else None
                timestamp = time.time()
                self.db.set_flag('send_after',str(timestamp+1.1))
                self.db.set_flag('last_send_kind','command')
            result,_ = self._transmit(text if formatted else escaped_clip(text,3800),telegram_id,version=version,timestamp=timestamp,reply_markup=reply_markup)
            return result

    def flush_command_reply(self):
        with self._send_lock:
            with self._lock:
                if self.stop_event.is_set() or float(self.db.get_flag('send_after','0')) > time.time():
                    return False
                row = self.db.pending_reply(time.time())
                if not row or not self.db.claim_command_reply(row['telegram_id'],time.time(),generation=row['generation']):
                    return False
            try:
                result = self.service_message(row['text'],telegram_id=row['telegram_id'], **({'reply_markup':json.loads(row['reply_markup']), 'formatted':True} if row['reply_markup'] else {}))
            except Exception:
                with self._lock:
                    self.db.set_reply_state(row['telegram_id'],'uncertain',generation=row['generation'])
                raise
            with self._lock:
                # New commands can replace the outbox row while this HTTP is in flight.
                # A result belongs only to the claimed generation, never to that newer row.
                if result is None:
                    self.db.set_reply_state(row['telegram_id'],'pending',generation=row['generation'])
                elif result.status == 'retry':
                    self.db.set_reply_state(row['telegram_id'],'pending',time.time()+max(10,result.retry_after),generation=row['generation'])
                else:
                    self.db.set_reply_state(row['telegram_id'],result.status,generation=row['generation'])
            return result is not None

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
        with self._send_lock:
            with self._lock:
                if self.stop_event.is_set() or self.db.get_flag('check_summary_pending') != '1':
                    return False
                if float(self.db.get_flag('send_after','0')) > time.time():
                    return False
                text = self.db.get_flag('check_summary','Итог проверки недоступен.')
                self.db.set_flag('check_summary_pending','0')
            try:
                result = self.service_message(text)
            except Exception:
                log.exception('Не удалось отправить итог /check; он сохранён для оператора')
                return True
            with self._lock:
                if result is None or result.status == 'retry':
                    self.db.set_flag('check_summary_pending','1')
            return result is not None

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
        report = None
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
                if report is not None:
                    self._last_report = dict(attempted=report['attempted'],successful=report['successful'])
                    self._scan_error = False
                if requested and not self.stop_event.is_set():
                    self.db.set_flag("check_summary", summary)
                    self.db.set_flag("check_summary_pending", "1")
                    self.db.set_flag("check_in_progress", "0")
                self._scan_running = False

    def _collection_scope(self):
        # Context-local settings follow nested collectors without mutable globals.
        return collection_scope(self.stop_event, budget=self.cfg.source_timeout_seconds,
                                request_timeout=self.cfg.request_timeout_seconds,
                                user_agent=self.cfg.user_agent)

    @contextmanager
    def _source_scope(self, key):
        with self._lock:
            source_lock = self._source_locks.setdefault(key,threading.RLock())
        with source_lock, self._collection_scope():
            identity=threading.get_ident()
            with self._lock:
                outer=identity not in self._source_deadlines
                if outer:self._source_deadlines[identity]=time.time()+self.cfg.source_timeout_seconds+2*self.cfg.request_timeout_seconds+30
            try:
                yield
            finally:
                if outer:
                    with self._lock:self._source_deadlines.pop(identity,None)

    def _defer_verification(self, key, source, timestamp):
        with self._lock:
            self._unverified_items.add(key)
            self._verify_after[key] = timestamp+60
            if key in self._verification_retries or len(self._verification_retries) < 128:
                self._verification_retries[key] = (timestamp+60,source)

    def _enqueue_verification_retries(self, timestamp):
        with self._lock:
            for key,(deadline,source) in list(self._verification_retries.items()):
                if deadline > timestamp:
                    continue
                job = ('post',key) if source.type == 'telegram_channel' else ('site',source.key)
                if job in self._verification_jobs:
                    continue
                try:
                    self._verification_queue.put_nowait((job,key,source))
                except queue.Full:
                    break
                self._verification_jobs.add(job)
                self._verification_retries.pop(key,None)

    def _verify_item(self, key, source, now):
        source_key = f'tg_{source.username.lower()}' if source.type == 'telegram_channel' else source.key
        with self._source_scope(source_key):
            with self._lock:
                generation = self._cache_generation
            if source.type == 'telegram_channel':
                entry = telegram_channel.fetch_post(source.username,int(key.rsplit(':',1)[1]))
                entries = [entry] if entry else []
                catalog,failed = {entry.item_key for entry in entries},set()
            else:
                snapshot = WEBSITE_COLLECTORS[source.type](source).fetch()
                entries = list(snapshot)
                catalog = set(snapshot.catalog) if isinstance(snapshot,WebsiteSnapshot) else {entry.item_key for entry in entries}
                failed = set(snapshot.failed_details) if isinstance(snapshot,WebsiteSnapshot) else set()
            with self._lock:
                if generation != self._cache_generation:
                    return False
                timestamp = max(now,time.time())
                if source.type != 'telegram_channel':
                    self._website_verified_at[source.key] = timestamp
                    self._website_failed[source.key] = failed
                    for failed_key in failed:
                        self._defer_verification(failed_key,source,timestamp)
                self._source_verify_after.pop(source.key,None)
                for entry in entries:
                    if self.db.exists(entry.item_key):
                        stored = self.db.item_row(entry.item_key)
                        self.process_item(entry,baseline=stored['delivery_state']=='baseline',
                                          cutoff=float(self.db.get_flag('source_snapshot:'+source.key,'0')))
                    self._fresh_items[entry.item_key] = entry
                    self._verified_at[entry.item_key] = timestamp
                    self._missing_items.discard(entry.item_key)
                    self._unverified_items.discard(entry.item_key)
                    self._verification_retries.pop(entry.item_key,None)
                if key in failed:
                    self._defer_verification(key,source,timestamp)
                    return False
                if key not in catalog:
                    self._fresh_items.pop(key,None)
                    self._verified_at[key] = timestamp
                    self._missing_items.add(key)
                    self._unverified_items.discard(key)
                    self._verification_retries.pop(key,None)
                self._verify_after.pop(key,None)
                return True

    def _verification_loop(self, stop):
        while not stop.is_set():
            self._enqueue_verification_retries(time.time())
            try:
                job,key,source = self._verification_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                verified=self._verify_item(key,source,time.time())
                if not verified:
                    with self._lock:
                        if key in self._unverified_items:self._defer_verification(key,source,time.time())
            except CollectionCancelled:
                return
            except Exception as exc:
                with self._lock:
                    self._defer_verification(key,source,time.time())
                    if source.type != 'telegram_channel':
                        self._source_verify_after[source.key] = time.time()+60
                log.warning('Проверка публикации источника %s отложена (%s)',source.key,type(exc).__name__)
            finally:
                with self._lock:
                    self._verification_jobs.discard(job)
                self._verification_queue.task_done()

    def _current_for_delivery(self, key, source, now):
        timestamp = now.timestamp()
        with self._lock:
            current = self._fresh_items.get(key)
            age = timestamp-self._verified_at.get(key,self._website_verified_at.get(source.key,self._last_scan))
            detail_failed = key in self._website_failed.get(source.key,set())
            if age < self.cfg.item_verification_ttl_seconds and not detail_failed:
                if current is not None or key in self._missing_items:
                    return True,current
                if source.type != 'telegram_channel':
                    return True,None  # absent from the recent complete website catalogue
            if timestamp < max(self._verify_after.get(key,0),self._source_verify_after.get(source.key,0)):
                return False,None
            if self._background_collection:
                job = ('post',key) if source.type == 'telegram_channel' else ('site',source.key)
                if job not in self._verification_jobs:
                    try:
                        self._verification_queue.put_nowait((job,key,source))
                    except queue.Full:
                        return False,None
                    self._verification_jobs.add(job)
                return False,None
        try:
            if not self._verify_item(key,source,timestamp):
                return False,None
        except CollectionCancelled:
            return False,None
        except Exception as exc:
            with self._lock:
                self._defer_verification(key,source,timestamp)
                if source.type != 'telegram_channel':
                    self._source_verify_after[source.key] = timestamp+60
            log.warning('Проверка публикации источника %s отложена (%s)',source.key,type(exc).__name__)
            return False,None
        with self._lock:
            return True,self._fresh_items.get(key)

    # ---------- сбор одного источника ----------
    def collect_source(self, key: str, name: str, fetch) -> list[Item] | None:
        try:
            with self._source_scope(key):
                items = fetch()
                check_collection()
            self.db.set_source_state(key, error=items.error_summary if isinstance(items,WebsiteSnapshot) else '')
            return items
        except CollectionCancelled:
            raise
        except Exception as e:  # noqa: BLE001 — ошибка источника не останавливает остальные
            log.error("источник %s: ошибка сбора: %s", key, e)
            self.db.set_source_state(key, error=str(e)[:300])
            return None

    # ---------- обработка найденного объявления ----------
    def process_item(self, item: Item, *, baseline: bool, cutoff=None) -> bool:
        """Возвращает True, если объявление новое, подходящее и подлежит отправке."""
        stored = self.db.item_row(item.item_key)
        fingerprint = item.content_hash
        if stored and stored['content_hash'] == fingerprint:
            return False
        ok, reason = self.filters.check(item)
        if stored:
            state = stored['delivery_state']
            # Accepted baseline and legacy terminal states remain non-broadcastable.
            if state == 'awaiting_detail':
                duplicate = ok and self.db.is_duplicate(item)
                state = 'rejected' if not ok else ('skipped' if duplicate else ('baseline' if baseline else 'pending'))
                self.db.revise_item(item,fingerprint,state=state,first_detail=True)
                return state == 'pending'
            if state not in {'pending','rejected','expired'} or not stored['content_hash']:
                self.db.revise_item(item,fingerprint,state=state)
                return False
            if not ok:
                self.db.revise_item(item,fingerprint,state='rejected')
                return False
            if state in {'rejected','expired'}:
                if self.db.is_duplicate(item):
                    self.db.revise_item(item,fingerprint,state='rejected')
                    return False
                # The edit time is unknown. Only users subscribed before the prior
                # complete snapshot join a reopened event; never replay a pause archive.
                cutoff = getattr(self,'_undated_cutoff',time.time()) if cutoff is None else cutoff
                self.db.revise_item(item,fingerprint,state='baseline' if baseline else 'pending',
                                    activate_at=None if baseline else cutoff)
                return not baseline
            self.db.revise_item(item,fingerprint,state=state)
            return False
        if not ok:
            log.debug("skip %s: %s", item.item_key, reason)
            # сохраняем как отброшенное, чтобы не проверять каждый час повторно
            self.db.add_item(item, baseline=True, category=None,content_hash=fingerprint)
            return False
        if self.db.is_duplicate(item):
            log.info("duplicate across sources: %s (%s)", item.item_key, item.title)
            self.db.add_item(item, baseline=True, category=item.categories[0],content_hash=fingerprint)
            self.db.set_delivery(item.item_key, "skipped", "duplicate")
            return False
        self.db.add_item(item, baseline=baseline, category=item.categories[0],content_hash=fingerprint,
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
        with self._send_lock:
            with self._lock:
                self.fill_delivery_queue(now)
                row = self.db.user_delivery(telegram_id,item.item_key)
                user = self.db.user(telegram_id)
                if (not row or row['status'] != 'pending' or row['available_at'] > now.timestamp()
                        or not user or not user['is_active'] or self.stop_event.is_set()):
                    return False
                if float(self.db.get_flag('send_after','0')) > now.timestamp():
                    return False
                accepted,reason = self.filters.check(item,now=now)
                if not accepted:
                    self.db.finish_user_delivery(row['id'],'expired',reason)
                    return False
                if not matches(self.db.preferences(telegram_id),item):
                    self.db.finish_user_delivery(row['id'],'skipped','personal_preferences')
                    return False
                self.db.refresh_pending(item)
                message = self.format_message(item)
                if not self.db.claim_user_delivery(row['id'],now):
                    return False
                # The durable claim is the point after which /stop cannot retract an attempt.
                self.db.set_flag('send_after',str(max(now.timestamp(),time.time())+1.1))
                self.db.set_flag('last_send_kind','event')
            try:
                result,completed = self._transmit(message,telegram_id,version=user['subscription_version'],timestamp=now.timestamp(),reply_markup=save_keyboard(self.db.item_ref(item.item_key)))
            except Exception:
                with self._lock:
                    self.db.finish_user_delivery(row['id'],'uncertain','unexpected_send_error')
                return False
            with self._lock:
                if result.status == 'sent':
                    self.db.finish_user_delivery(row['id'],'sent','delivered',message_id=result.message_id)
                    self._sent_total += 1
                    return True
                if result.status == 'retry':
                    current = self.db.user(telegram_id)
                    if not current or not current['is_active'] or current['subscription_epoch'] != user['subscription_epoch']:
                        self.db.finish_user_delivery(row['id'],'skipped','subscription_changed_during_send')
                    else:
                        attempts = row['attempts']+1
                        delay = (result.retry_after if result.code == 429 else
                                 max(result.retry_after,min(3600,5*2**min(attempts,10))))
                        self.db.finish_user_delivery(row['id'],'pending','telegram_retry',available_at=completed+delay)
                else:
                    self.db.finish_user_delivery(row['id'],result.status,f'telegram_{result.code}')
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
        self.db.fill_user_queue(available_sources=ready, configured_sources=configured, blocked_items=self._unverified_items)

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
            ready,current = self._current_for_delivery(row['item_key'],sources[row['source_key']],now)
            if not ready:
                continue
            if current is None:
                self.db.finish_user_delivery(row["delivery_id"],"expired","publication_no_longer_present")
                continue
            if self.deliver(current,telegram_id=row["telegram_id"],now=now):
                sent+=1
            if self.stop_event.is_set():
                break
        return sent

    def flush_reminder(self, *, now=None):
        now = now or datetime.now(timezone.utc)
        with self._send_lock:
            with self._lock:
                if self.stop_event.is_set() or float(self.db.get_flag('send_after','0')) > now.timestamp():
                    return False
                job = self.db.claim_reminder(now.timestamp())
                if not job:
                    return False
                item = self.db.deserialize(job['payload'])
                if item.explicitly_closed or item.registration_status == 'closed':
                    self.db.finish_reminder(job,'cancelled')
                    return False
                message = format_reminder(item,job,self.db.preferences(job['telegram_id']))
                self.db.set_flag('send_after',str(max(now.timestamp(),time.time())+1.1))
                self.db.set_flag('last_send_kind','reminder')
            try:
                result,completed = self._transmit(message,job['telegram_id'],
                    version=job['subscription_version'],timestamp=now.timestamp(),
                    reply_markup=save_keyboard(job['item_id']))
            except Exception:
                log.exception('Ошибка отправки напоминания; результат uncertain')
                with self._lock:self.db.finish_reminder(job,'uncertain')
                return True
            with self._lock:
                self.db.finish_reminder(job,result.status,completed+max(10,result.retry_after),result.message_id)
            return True

    def dispatch_pending(self, *, now=None):
        """Round robin HTTP slots across commands, publications and reminders."""
        now = now or datetime.now(timezone.utc)
        if self.stop_event.is_set() or float(self.db.get_flag('send_after','0')) > now.timestamp():
            return 0
        self.fill_delivery_queue(now)
        kinds = ['command','event','reminder']
        last = self.db.get_flag('last_send_kind')
        index = (kinds.index(last)+1) % 3 if last in kinds else 0
        for kind in kinds[index:]+kinds[:index]:
            if kind == 'command':
                if self.flush_command_reply() or self.flush_check_summary():
                    return 0
                sent = 0
            elif kind == 'event':
                sent = self.drain_pending(now=now)
            else:
                self.flush_reminder(now=now)
                sent = 0
            if float(self.db.get_flag('send_after','0')) > now.timestamp():
                return sent
        return 0

    def _run_cycle(self):
        started = datetime.now(timezone.utc)
        found = new_count = notified = 0
        sent_before = self._sent_total
        errors: list[str] = []
        checked_keys = []
        successful = matched = 0
        with self._lock:
            self._cache_generation += 1
            configured=self.delivery_sources()
            now=time.time()
            for key,source in configured.items():
                if source.type != 'telegram_channel' and key in self._available_sources:
                    self._website_verified_at.setdefault(key,self._last_scan)
            for key in set(self._fresh_items)|self._missing_items:
                entry=self._fresh_items.get(key)
                if (now-self._verified_at.get(key,self._last_scan) >= self.cfg.item_verification_ttl_seconds
                        or (entry is not None and entry.source_key not in configured)):
                    self._fresh_items.pop(key,None)
                    self._verified_at.pop(key,None)
                    self._missing_items.discard(key)
            self._available_sources.intersection_update(configured)
            self._last_scan=now
        self._cycle_started = started.timestamp()

        def handle_source(key: str, items: list[Item], *, initial_snapshot: bool = False, known_cursor=None, source=None) -> tuple[int, int]:
            nonlocal matched
            first = not self.db.source_initialized(key)
            # Undated website publications need a successful personal baseline:
            # the first snapshot after subscription/resume cannot establish recency.
            self._undated_cutoff = float(self.db.get_flag('source_snapshot:'+key, '0'))
            new_items=0
            if source is not None and source.type != 'telegram_channel':
                catalog=set(items.catalog) if isinstance(items,WebsiteSnapshot) else {it.item_key for it in items}
                failures=set(items.failed_details) if isinstance(items,WebsiteSnapshot) else set()
                with self._lock:
                    self._website_verified_at[key]=time.time()
                    self._website_failed[key]=failures
                    for item_key in list(self._fresh_items):
                        if self._fresh_items[item_key].source_key == key and item_key not in catalog:
                            self._fresh_items.pop(item_key,None)
                            self._missing_items.add(item_key)
                            self._verified_at[item_key]=time.time()
                    for item_key in list(self._unverified_items):
                        if item_key.startswith(key+':') and item_key not in failures:
                            self._unverified_items.discard(item_key)
                            self._verification_retries.pop(item_key,None)
            if isinstance(items,WebsiteSnapshot):
                self._unverified_items.update(items.failed_details)
                self.db.record_unread_details(key,items.catalog,items.failed_details,
                    baseline=initial_snapshot,eligible_at=self._undated_cutoff)
            for it in items:
                check_collection()
                with self._lock:
                    self._fresh_items[it.item_key]=it
                    self._verified_at[it.item_key]=time.time()
                    self._unverified_items.discard(it.item_key)
                    self._verification_retries.pop(it.item_key,None)
                    self._missing_items.discard(it.item_key)
                    already_seen = self.db.exists(it.item_key)
                    baseline = initial_snapshot or (known_cursor is not None and not already_seen
                                                    and int(it.external_id) <= known_cursor)
                    became_eligible = self.process_item(it,baseline=baseline)
                    if became_eligible:
                        new_items+=1
                    if (not already_seen or became_eligible) and not initial_snapshot:
                        row = self.db.item_row(it.item_key)
                        if row and row['delivery_state'] in {'pending', 'baseline'}:
                            matched += 1
            check_collection()
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
                with self._source_scope(source_key):
                    items = telegram_channel.fetch_channel(ch.username, since_id=cursor, max_pages=ch.max_pages, include_recent=True)
                    n_found, n_new = handle_source(ch.key, items, initial_snapshot=cursor is None, known_cursor=cursor, source=ch)
                    if items:
                        self.db.save_telegram_cursor(source_key, max(cursor or 0, max(int(it.external_id) for it in items)))
                    self.db.set_source_state(ch.key, error="")
                self._available_sources.add(source_key)
                successful += 1
                found += n_found
                new_count += n_new
            except CollectionCancelled:
                break
            except Exception as exc:
                with self._lock:self._available_sources.discard(source_key)
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
            try:
                with self._source_scope(w.key):
                    items = self.collect_source(w.key, w.name, collector.fetch)
                    if items is None:
                        with self._lock:self._available_sources.discard(w.key)
                        continue
                    n_found, n_new = handle_source(w.key, items, initial_snapshot=not self.db.source_initialized(w.key), source=w)
            except CollectionCancelled:
                break
            except CollectionTimeout as exc:
                with self._lock:self._available_sources.discard(w.key)
                self.db.set_source_state(w.key,error=str(exc))
                continue
            self._available_sources.add(w.key)
            successful += 1
            found += n_found
            new_count += n_new

        if not self._background_collection:
            self.dispatch_pending()
        notified = self._sent_total-sent_before

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

    def _write_health(self):
        with self._lock:
            state=dict(version=1,ready=self._health_ready,updated_at=time.time(),
                started_at=self._started_at,dispatcher_at=self._dispatcher_at,
                poll_expected=self._poll_expected,poll_at=self._poll_at,poll_retry_until=self._poll_retry_until,
                source_deadlines=list(self._source_deadlines.values()),scan_report=self._last_report,
                scan_error=self._scan_error,queue=self.db.health_metrics(include_commands=True),
                max_queue_age=self.cfg.max_queue_age_seconds,backup_enabled=bool(self.cfg.backup_dir),
                backup_error=self.backup_error,last_backup_at=float(self.db.get_flag('last_backup_at','0')),
                backup_interval=self.cfg.backup_interval_seconds,
                disk_free_bytes=free_disk([Path(self.cfg.db_path).parent]+([self.cfg.backup_dir] if self.cfg.backup_dir else [])),
                min_free_disk_bytes=self.cfg.min_free_disk_bytes)
        write_health(health_path(self.cfg.db_path),state)
        self._health_write_at=time.monotonic()

    def _backup_loop(self, stop):
        next_attempt = 0.0
        while not stop.is_set():
            now = time.time()
            last = float(self.db.get_flag('last_backup_at','0'))
            if now >= next_attempt and (now-last >= self.cfg.backup_interval_seconds or last > now):
                try:
                    create_backup(self.cfg.db_path,self.cfg.backup_dir,keep=self.cfg.backup_keep,stop=stop)
                    self.db.set_flag('last_backup_at',str(time.time()))
                    self.backup_error = None
                    log.info('Плановая резервная копия SQLite проверена и сохранена')
                except BackupCancelled:
                    return
                except Exception as exc:
                    self.backup_error = type(exc).__name__
                    log.error('Резервное копирование отложено (%s)',type(exc).__name__)
                next_attempt = time.time()+min(300,self.cfg.backup_interval_seconds)
            stop.wait(min(60,self.cfg.backup_interval_seconds))

    def _scan_loop(self, stop):
        next_scan = 0.0
        while not stop.is_set():
            if self.scan_requested.is_set():
                self.scan_requested.clear()
                next_scan = 0.0
            if time.monotonic() >= next_scan:
                try:
                    self.run_cycle()
                except Exception:
                    self._scan_error = True
                    log.exception("Ошибка сбора; состояние источников сохранено")
                next_scan = time.monotonic()+self.cfg.poll_interval_seconds
            stop.wait(0.2)

    def loop(self, stop):
        # Only the scanner collects full snapshots. The calling thread services
        # the durable outboxes while a source is waiting on network I/O.
        self._background_collection = True
        self._health_ready = True
        self._health_write_at = 0.0
        scanner = threading.Thread(target=self._scan_loop,args=(stop,),daemon=True,name='collector')
        verifier = threading.Thread(target=self._verification_loop,args=(stop,),daemon=True,name='verifier')
        workers = [scanner,verifier]
        if self.cfg.backup_dir:
            workers.append(threading.Thread(target=self._backup_loop,args=(stop,),daemon=True,name='backup'))
        for worker in workers:worker.start()
        try:
            while not stop.is_set():
                self._dispatcher_at=time.time()
                try:
                    self.dispatch_pending()
                except Exception:
                    log.exception("Ошибка доставки; состояние очереди сохранено")
                if time.monotonic()-self._health_write_at >= 10:
                    try:self._write_health()
                    except Exception as exc:log.error('Heartbeat недоступен (%s)',type(exc).__name__)
                stop.wait(2)
        finally:
            stop.set()
            for worker in workers:worker.join()
            self._background_collection = False
            self._health_ready = False
            try:self._write_health()
            except Exception:pass


HELP = ("/app — открыть IT Radar Mini App\n/settings — персональные фильтры\n/saved — избранное\n/week — афиша на неделю\n/reminders — напоминания\n"
        "/start — включить уведомления\n/status — состояние подписки\n"
        "/stop — отключить уведомления\n/help — помощь")
WELCOME = ("Привет! 👋\n\nЯ IT Radar — бот, который помогает находить стажировки, "
           "хакатоны, конференции, конкурсы и другие возможности в IT.\n\n"
           "🔎 Я отслеживаю публикации из Telegram-каналов и карьерных сайтов.\n"
           "🔔 Уведомления включены! Архив не рассылается.\n\n" + HELP)


def help_for(bot, telegram_id):
    """List commands available to this sender, including owner-only actions."""
    if telegram_id == bot.cfg.owner_id and telegram_id > 0:
        return HELP + "\n/check — проверить источники сейчас (только владелец)"
    return HELP


def handle_update(bot, upd):
    identifier = upd.get('update_id')
    if type(identifier) is not int or identifier < 0:
        return
    if 'callback_query' in upd:
        handle_callback(bot,upd)
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
        markup = None
        if authorized:
            help_text = help_for(bot, telegram_id)
            if command == '/start':
                active = True
                reply = WELCOME + help_text[len(HELP):]
                markup = launch_keyboard(bot.cfg.miniapp_url)
            elif command == '/app':
                markup = launch_keyboard(bot.cfg.miniapp_url)
                reply = ('Каталог IT Radar и ваше избранное — в Mini App.' if markup
                         else 'Mini App пока не настроен. Основные команды бота продолжают работать. /help — помощь.')
            elif command == '/stop':
                active = False
                reply = 'Уведомления отключены, будущие напоминания отменены. Избранное сохранено. /start — возобновить без рассылки архива.'
            elif command in ('/settings','/saved','/week','/reminders'):
                reply,markup=render(bot.db,telegram_id,command[1:])
            elif not command.startswith('/') and isinstance(text,str) and bot.db.get_flag(f'preference_input:{telegram_id}',''):
                reply,markup=consume_input(bot,telegram_id,text)
            elif command == '/status':
                user = bot.db.user(telegram_id)
                counts = bot.db.user_delivery_counts(telegram_id)
                reply = (f"Уведомления: {'включены' if user and user['is_active'] else 'выключены'}.\n"
                         f"Ожидают отправки: {counts.get('pending',0)}.\n"
                         f"Отправляются: {counts.get('sending',0)}.\n"
                         f"Приостановлены из-за источников: {counts.get('suspended',0)}.\n"
                         f"Неизвестный результат: {counts.get('uncertain',0)}.\n" + help_text)
            elif command == '/help':
                reply = 'IT Radar присылает новые подходящие IT-возможности.\n' + help_text
                markup = launch_keyboard(bot.cfg.miniapp_url)
            elif command == '/check' and telegram_id == bot.cfg.owner_id:
                accepted, reply = bot.check_request_reply(time.time())
                if accepted:
                    bot.db.apply_control(check_requested_at=time.time())
                    bot.scan_requested.set()
            else:
                reply = 'Неизвестная команда.\n' + help_text
        bot.db.user_control(telegram_id if authorized else 0,
                            sender.get('username') if isinstance(sender.get('username'),str) else None,
                            active=active, offset=identifier+1, reply=reply, reply_markup=markup)
        # HTTP replies are sent by the shared scheduler; commands only persist state.


def poll_updates(bot, stop):
    while not stop.is_set():
        try:
            bot._poll_at=time.time()
            bot._poll_retry_until=0.0
            updates=bot.notifier.call("getUpdates",{"offset":int(bot.db.get_flag("offset","0")),
                                      "timeout":25,"allowed_updates":["message","callback_query"]},timeout=35)
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
            bot._poll_retry_until=time.time()+max(5,exc.retry_after)
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
    parser.add_argument('--accept-telegram-gap', metavar='USERNAME', help='Принять пропуск удалённой истории в остановленной БД; без сети')
    parser.add_argument('--through-id', type=int, help='Последний ID сознательно пропускаемой истории')
    args = parser.parse_args()
    if bool(args.accept_telegram_gap) != (args.through_id is not None):
        parser.error('--accept-telegram-gap и --through-id задаются вместе')
    if bool(args.resolve_item) != bool(args.resolution):
        parser.error("--resolve-item и --resolution задаются вместе")
    if sum(bool(x) for x in (args.check_config,args.delivery_status,args.resolve_item,args.accept_telegram_gap)) > 1:
        parser.error("Выберите один режим диагностики")
    configure_logging()
    try:
        cfg = Config.load()
        if not args.delivery_status and not args.resolve_item and not args.accept_telegram_gap:
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
    if args.delivery_status or args.resolve_item or args.accept_telegram_gap:
        if not Path(cfg.db_path).is_file():
            raise RuntimeError("Database does not exist; set DB_PATH")
        db=Database(cfg.db_path, metadata_timezone=cfg.rules.timezone)
        try:
            if args.accept_telegram_gap:
                names = {channel.username.lower() for channel in cfg.channels}
                if args.accept_telegram_gap.lower() not in names:
                    raise ValueError('Укажите username настроенного Telegram-источника')
                db.accept_telegram_gap('tg_'+args.accept_telegram_gap.lower(),args.through_id)
                log.info('Пропуск истории явно принят; сообщения не отправлялись.')
                return
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

    bot._write_health()
    try:
        bot.initialize()
    except Exception:
        bot.db.close()
        raise
    poller = threading.Thread(target=poll_updates, args=(bot, stop), daemon=True, name="poller")
    bot._poll_expected = True
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

