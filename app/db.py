"""SQLite: хранилище просмотренных публикаций, состояний источников и статуса доставки."""
from __future__ import annotations

import sqlite3
import json
import os
import time
import fcntl
from pathlib import Path
from dataclasses import asdict
from app.models import Item
from app.dedup import identities
import threading
from datetime import datetime, timezone

SCHEMA_VERSION = 6
COMMAND_REPLY_INTERVAL = 5.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen_items (
    item_key       TEXT PRIMARY KEY,      -- source_key:external_id
    source_key     TEXT NOT NULL,
    external_id    TEXT NOT NULL,
    url            TEXT,
    title          TEXT,
    summary        TEXT,
    apply_url      TEXT,
    category       TEXT,
    title_hash     TEXT,                  -- нормализованное название (кросс-дедуп)
    published_at   TEXT,
    first_seen_at  TEXT NOT NULL,
    notified       INTEGER NOT NULL DEFAULT 0,
    baseline       INTEGER NOT NULL DEFAULT 0  -- 1 = запомнен при первом запуске, не присылать
);
CREATE INDEX IF NOT EXISTS idx_seen_title_hash ON seen_items(title_hash);
CREATE INDEX IF NOT EXISTS idx_seen_notified ON seen_items(notified);

CREATE TABLE IF NOT EXISTS source_state (
    source_key     TEXT PRIMARY KEY,
    last_check_at  TEXT,
    last_error     TEXT,
    initialized    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS runs (
    started_at     TEXT,
    finished_at    TEXT,
    items_found    INTEGER,
    items_new      INTEGER,
    items_notified INTEGER,
    errors         TEXT
);

CREATE TABLE IF NOT EXISTS flags (
    name  TEXT PRIMARY KEY,
    value TEXT
);
"""


class Database:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._file_lock = None
        self._conn = None
        self.backup_path = None
        try:
            if path != ":memory:":
                Path(path).parent.mkdir(parents=True, exist_ok=True)
                self._file_lock = open(path + ".lock", "a")
                try:
                    fcntl.flock(self._file_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise RuntimeError("Database is already in use by another bot process") from None
            self._conn = sqlite3.connect(path, timeout=10, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute('CREATE TEMP TABLE blocked_delivery_items(item_key TEXT PRIMARY KEY)')
            self._conn.execute("PRAGMA foreign_keys=ON")
            version = self._conn.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise RuntimeError("Database schema is newer than this application")
            existing = {row[1] for row in self._conn.execute("PRAGMA table_info(seen_items)")}
            if existing and version < SCHEMA_VERSION and path != ":memory:":
                self.backup_path = path + ".before-delivery-" + str(time.time_ns()) + ".bak"
                backup = sqlite3.connect(self.backup_path)
                try:
                    self._conn.backup(backup)
                finally:
                    backup.close()
                os.chmod(self.backup_path, 0o600)
            self._conn.executescript(SCHEMA)
            with self._conn:
                columns = {row[1] for row in self._conn.execute("PRAGMA table_info(seen_items)")}
                for name, definition in {
                    "payload": "TEXT", "delivery_state": "TEXT NOT NULL DEFAULT 'legacy'",
                    "delivery_reason": "TEXT", "available_at": "REAL NOT NULL DEFAULT 0",
                    "message_id": "INTEGER", "attempts": "INTEGER NOT NULL DEFAULT 0",
                    "content_hash": "TEXT", "revision": "INTEGER NOT NULL DEFAULT 1",
                    "opportunity_at": "TEXT", "content_updated_at": "TEXT",
                }.items():
                    if name not in columns:
                        self._conn.execute(f"ALTER TABLE seen_items ADD COLUMN {name} {definition}")
                if 'opportunity_at' not in columns:
                    self._conn.execute('UPDATE seen_items SET opportunity_at=published_at')
                for stored in self._conn.execute('SELECT item_key,payload FROM seen_items WHERE content_hash IS NULL AND payload IS NOT NULL'):
                    self._conn.execute('UPDATE seen_items SET content_hash=? WHERE item_key=?',
                                       (self.deserialize(stored['payload']).content_hash,stored['item_key']))
                self._conn.execute("""UPDATE seen_items SET delivery_state=CASE
                    WHEN baseline=1 THEN 'baseline' WHEN notified=1 THEN 'sent'
                    ELSE 'legacy_review' END, delivery_reason='migrated'
                    WHERE delivery_state='legacy'""")
                self._conn.execute("""UPDATE seen_items SET delivery_state='uncertain',
                    delivery_reason='interrupted_during_send' WHERE delivery_state='sending'""")
                self._conn.execute("""CREATE TABLE IF NOT EXISTS item_aliases (
                    alias TEXT NOT NULL, item_key TEXT NOT NULL,
                    PRIMARY KEY(alias,item_key))""")
                self._conn.execute("CREATE INDEX IF NOT EXISTS idx_delivery_state ON seen_items(delivery_state,available_at)")
                # Backfill strong public URL identities for old accepted rows;
                # titles alone must never suppress another company's vacancy.
                if self._flag('legacy_aliases_backfilled','') != '1':
                    legacy = self._conn.execute("SELECT * FROM seen_items WHERE payload IS NULL AND category IS NOT NULL")
                    while True:
                        rows = legacy.fetchmany(500)
                        if not rows:
                            break
                        for row in rows:
                            old = Item(row['source_key'],row['source_key'],row['external_id'],row['url'] or '',
                                       row['title'] or '',row['summary'] or '',row['apply_url'],None)
                            for alias in identities(old):
                                self._conn.execute("INSERT OR IGNORE INTO item_aliases VALUES(?,?)",(alias,row['item_key']))
                    self._put_flag('legacy_aliases_backfilled','1')
                self._conn.execute("""CREATE TABLE IF NOT EXISTS users (
                    telegram_id INTEGER PRIMARY KEY, username TEXT,
                    is_active INTEGER NOT NULL DEFAULT 0 CHECK(is_active IN (0,1)),
                    created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    subscribed_at REAL NOT NULL)""")
                user_columns = {row[1] for row in self._conn.execute('PRAGMA table_info(users)')}
                for name in ('subscription_version','subscription_epoch'):
                    if name not in user_columns:
                        self._conn.execute(f'ALTER TABLE users ADD COLUMN {name} INTEGER NOT NULL DEFAULT 1')
                self._conn.execute("""CREATE TABLE IF NOT EXISTS user_deliveries (
                    id INTEGER PRIMARY KEY, telegram_id INTEGER NOT NULL REFERENCES users(telegram_id),
                    item_key TEXT NOT NULL REFERENCES seen_items(item_key),
                    status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    available_at REAL NOT NULL DEFAULT 0, sent_at REAL,
                    telegram_message_id INTEGER, reason TEXT,
                    UNIQUE(telegram_id,item_key))""")
                self._conn.execute("CREATE INDEX IF NOT EXISTS idx_active_users ON users(is_active,subscribed_at)")
                self._conn.execute("CREATE INDEX IF NOT EXISTS idx_user_queue ON user_deliveries(status,available_at,id)")
                self._conn.execute("CREATE INDEX IF NOT EXISTS idx_delivery_status_id ON user_deliveries(status,id)")
                self._conn.execute("CREATE INDEX IF NOT EXISTS idx_user_status ON user_deliveries(telegram_id,status)")
                self._conn.execute("CREATE INDEX IF NOT EXISTS idx_delivery_item_status ON user_deliveries(item_key,status)")
                if "eligible_at" not in columns:
                    self._conn.execute("ALTER TABLE seen_items ADD COLUMN eligible_at REAL")
                self._conn.execute("CREATE INDEX IF NOT EXISTS idx_item_eligibility ON seen_items(eligible_at)")
                self._conn.execute("UPDATE user_deliveries SET status='uncertain',reason='interrupted_during_send' WHERE status='sending'")
                self._conn.execute("""CREATE TABLE IF NOT EXISTS command_replies (
                    telegram_id INTEGER PRIMARY KEY, text TEXT NOT NULL, status TEXT NOT NULL,
                    available_at REAL NOT NULL DEFAULT 0, updated_at REAL NOT NULL)""")
                if 'generation' not in {row[1] for row in self._conn.execute('PRAGMA table_info(command_replies)')}:
                    self._conn.execute('ALTER TABLE command_replies ADD COLUMN generation INTEGER NOT NULL DEFAULT 1')
                largest = self._conn.execute('SELECT COALESCE(MAX(generation),0) FROM command_replies').fetchone()[0]
                sequence = self._conn.execute("SELECT value FROM flags WHERE name='reply_generation'").fetchone()
                generation = max(largest,int(sequence[0]) if sequence else 0)
                self._conn.execute("""INSERT INTO flags VALUES('reply_generation',?)
                    ON CONFLICT(name) DO UPDATE SET value=excluded.value""",(str(generation),))
                self._conn.execute("UPDATE command_replies SET status='uncertain' WHERE status='sending'")
                self._conn.execute("""CREATE TABLE IF NOT EXISTS command_reply_limits (
                    telegram_id INTEGER PRIMARY KEY, reply_after REAL NOT NULL)""")
                fanout_exists = self._conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='delivery_fanout'").fetchone()
                self._conn.execute("""CREATE TABLE IF NOT EXISTS delivery_fanout (
                    id INTEGER PRIMARY KEY, item_key TEXT NOT NULL UNIQUE REFERENCES seen_items(item_key),
                    source_key TEXT NOT NULL, cursor_id INTEGER NOT NULL DEFAULT 0,
                    audience_max_id INTEGER NOT NULL, priority INTEGER NOT NULL DEFAULT 0)""")
                self._conn.execute('CREATE INDEX IF NOT EXISTS idx_fanout_source ON delivery_fanout(source_key,priority,id)')
                if not fanout_exists:
                    self._conn.execute("INSERT INTO flags VALUES('queue_dirty','1') ON CONFLICT(name) DO UPDATE SET value='1'")
                    # Reconcile the old lazy queue once, in bounded batches after startup.
                    # Fresh events have priority over this migration backlog.
                    self._conn.execute("""INSERT INTO delivery_fanout(item_key,source_key,audience_max_id,priority)
                        SELECT item_key,source_key,COALESCE((SELECT MAX(telegram_id) FROM users),0),1
                        FROM seen_items WHERE delivery_state='pending' AND eligible_at IS NOT NULL
                        ORDER BY eligible_at,item_key""")
                self._conn.execute("INSERT OR IGNORE INTO flags VALUES('queue_dirty','1')")
                self._conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        except Exception:
            self.close()
            raise

    def _flag(self, name, default='0'):
        row = self._conn.execute('SELECT value FROM flags WHERE name=?',(name,)).fetchone()
        return row[0] if row else default

    def _put_flag(self, name, value):
        self._conn.execute("INSERT INTO flags VALUES(?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value",(name,str(value)))

    def _schedule_fanout(self, item_key, *, reset=False):
        if reset:
            self._conn.execute('DELETE FROM delivery_fanout WHERE item_key=?',(item_key,))
        self._conn.execute("""INSERT OR IGNORE INTO delivery_fanout(item_key,source_key,audience_max_id)
            SELECT item_key,source_key,COALESCE((SELECT MAX(telegram_id) FROM users),0)
            FROM seen_items WHERE item_key=? AND delivery_state='pending' AND eligible_at IS NOT NULL""",(item_key,))

    def migrate_owner(self, owner_id):
        """One transaction; legacy queue remains available for audit, never replayed globally."""
        with self._lock, self._conn:
            if self._conn.execute("SELECT 1 FROM flags WHERE name='multi_owner_migrated'").fetchone():
                return
            saved = self._conn.execute("SELECT value FROM flags WHERE name='owner_id'").fetchone()
            owner_id = int(saved[0]) if saved else owner_id
            legacy = (saved or self._conn.execute("SELECT 1 FROM flags WHERE name IN ('enabled','ever_started')").fetchone()
                      or self._conn.execute("SELECT 1 FROM seen_items LIMIT 1").fetchone())
            if legacy and not owner_id:
                raise RuntimeError("Legacy database has no saved owner ID; set the previous OWNER_TELEGRAM_ID for migration")
            if owner_id and legacy:
                enabled = self._conn.execute("SELECT value FROM flags WHERE name='enabled'").fetchone()
                now = time.time()
                self._conn.execute("INSERT OR IGNORE INTO users(telegram_id,username,is_active,created_at,updated_at,subscribed_at) VALUES(?,NULL,?,?,?,?)",
                                   (owner_id, int(bool(enabled and enabled[0]=='1')), now, now, 0))
                self._conn.execute("""INSERT OR IGNORE INTO user_deliveries
                    (telegram_id,item_key,status,attempts,available_at,telegram_message_id,reason,sent_at)
                    SELECT ?,item_key,delivery_state,attempts,available_at,message_id,
                    delivery_reason,NULL FROM seen_items""", (owner_id,))
            self._conn.execute("""UPDATE user_deliveries SET status='skipped',reason='migrated_paused'
                WHERE status='pending' AND telegram_id IN (SELECT telegram_id FROM users WHERE is_active=0)""")
            self._put_flag('queue_dirty','1')
            self._conn.execute("INSERT INTO flags VALUES('multi_owner_migrated','1')")

    def has_users(self, *, active_only=False):
        with self._lock:
            query = "SELECT 1 FROM users" + (" WHERE is_active=1" if active_only else "") + " LIMIT 1"
            return self._conn.execute(query).fetchone() is not None

    def user(self, telegram_id):
        with self._lock:
            return self._conn.execute("SELECT * FROM users WHERE telegram_id=?", (telegram_id,)).fetchone()

    def user_control(self, telegram_id, username=None, *, active=None, offset=None, reply=None):
        """Subscription and polling offset commit before the acknowledgement HTTP call."""
        now = time.time()
        with self._lock, self._conn:
            if active is not None:
                self._conn.execute("""INSERT INTO users(telegram_id,username,is_active,created_at,updated_at,subscribed_at) VALUES(?,?,?,?,?,?)
                    ON CONFLICT(telegram_id) DO UPDATE SET username=excluded.username,
                    subscribed_at=CASE WHEN users.is_active=0 AND excluded.is_active=1
                        THEN excluded.subscribed_at ELSE users.subscribed_at END,
                    subscription_version=users.subscription_version+1,
                    subscription_epoch=users.subscription_epoch+CASE WHEN users.is_active<>excluded.is_active THEN 1 ELSE 0 END,
                    is_active=excluded.is_active,updated_at=excluded.updated_at""",
                    (telegram_id,username,int(active),now,now,now))
                if not active:
                    self._conn.execute("UPDATE user_deliveries SET status='skipped',reason='paused' WHERE telegram_id=? AND status IN ('pending','suspended')", (telegram_id,))
            if reply is not None:
                sequence = self._conn.execute("SELECT value FROM flags WHERE name='reply_generation'").fetchone()
                generation = int(sequence[0])+1
                self._conn.execute("UPDATE flags SET value=? WHERE name='reply_generation'",(str(generation),))
                # One outstanding answer per chat; a newer command replaces an older answer.
                self._conn.execute("DELETE FROM command_replies WHERE status='pending' AND telegram_id<>? AND telegram_id IN (SELECT telegram_id FROM command_replies WHERE status='pending' ORDER BY updated_at DESC LIMIT -1 OFFSET 999)", (telegram_id,))
                self._conn.execute("""INSERT INTO command_replies(telegram_id,text,status,available_at,updated_at,generation) VALUES(?,?,'pending',0,?,?)
                    ON CONFLICT(telegram_id) DO UPDATE SET text=excluded.text,status='pending',
                    available_at=0,updated_at=excluded.updated_at,generation=excluded.generation""", (telegram_id,reply,now,generation))
            if offset is not None:
                self._conn.execute("INSERT INTO flags VALUES('offset',?) ON CONFLICT(name) DO UPDATE SET value=excluded.value", (str(offset),))

    def deactivate_if_version(self, telegram_id, version):
        """A late 403 must not undo a newer subscription command."""
        with self._lock, self._conn:
            changed = self._conn.execute("""UPDATE users SET is_active=0,updated_at=?,
                subscription_version=subscription_version+1,subscription_epoch=subscription_epoch+1
                WHERE telegram_id=? AND is_active=1 AND subscription_version=?""",
                (time.time(),telegram_id,version)).rowcount == 1
            if changed:
                self._conn.execute("""UPDATE user_deliveries SET status='skipped',reason='blocked'
                    WHERE telegram_id=? AND status IN ('pending','suspended')""",(telegram_id,))
            return changed

    def pending_reply(self, now):
        with self._lock:
            return self._conn.execute("""SELECT r.* FROM command_replies r
                LEFT JOIN command_reply_limits l ON l.telegram_id=r.telegram_id
                WHERE r.status='pending' AND r.available_at<=? AND COALESCE(l.reply_after,0)<=?
                ORDER BY r.updated_at,r.telegram_id LIMIT 1""", (now,now)).fetchone()

    def claim_command_reply(self, telegram_id, now, *, generation=None):
        """Claim once and persist the per-chat response limit before the HTTP attempt."""
        with self._lock, self._conn:
            claimed = self._conn.execute("""UPDATE command_replies SET status='sending'
                WHERE telegram_id=? AND status='pending' AND available_at<=?
                AND (? IS NULL OR generation=?)
                AND COALESCE((SELECT reply_after FROM command_reply_limits WHERE telegram_id=?),0)<=?""",
                (telegram_id,now,generation,generation,telegram_id,now)).rowcount == 1
            if claimed:
                self._conn.execute('DELETE FROM command_reply_limits WHERE reply_after<=?', (now,))
                self._conn.execute("""INSERT INTO command_reply_limits VALUES(?,?)
                    ON CONFLICT(telegram_id) DO UPDATE SET reply_after=excluded.reply_after""",
                    (telegram_id,now+COMMAND_REPLY_INTERVAL))
            return claimed

    def set_reply_state(self, telegram_id, status, available_at=0, *, generation=None):
        with self._lock, self._conn:
            if status == 'sent':
                self._conn.execute("DELETE FROM command_replies WHERE telegram_id=? AND (? IS NULL OR generation=?)",
                                   (telegram_id,generation,generation))
            else:
                self._conn.execute("""UPDATE command_replies SET status=?,available_at=?
                    WHERE telegram_id=? AND (? IS NULL OR generation=?)""",
                    (status,available_at,telegram_id,generation,generation))

    def _reconcile_queue(self, configured, available, blocked):
        """Reclassify live tasks only when readiness or a direct retry changed."""
        signature = json.dumps([configured,available,sorted(blocked)],ensure_ascii=False)
        if self._flag('queue_availability','') == signature and self._flag('queue_dirty') == '0':
            return
        if configured is not None:
            if configured:
                placeholders = ','.join('?' for _ in configured)
                self._conn.execute(f"""UPDATE user_deliveries AS d SET status='skipped',
                    reason='source_disabled_or_unverified' WHERE status IN ('pending','suspended')
                    AND NOT EXISTS(SELECT 1 FROM seen_items s WHERE s.item_key=d.item_key
                        AND s.source_key IN ({placeholders}))""",configured)
            else:
                self._conn.execute("""UPDATE user_deliveries SET status='skipped',
                    reason='source_disabled_or_unverified' WHERE status IN ('pending','suspended')""")
        if available is not None:
            if available:
                placeholders = ','.join('?' for _ in available)
                self._conn.execute(f"""UPDATE user_deliveries AS d SET status='suspended',
                    reason='source_unavailable' WHERE status='pending' AND NOT EXISTS
                    (SELECT 1 FROM seen_items s WHERE s.item_key=d.item_key
                        AND s.source_key IN ({placeholders}))""",available)
            else:
                self._conn.execute("UPDATE user_deliveries SET status='suspended',reason='source_unavailable' WHERE status='pending'")
        self._conn.execute("""UPDATE user_deliveries SET status='suspended',reason='detail_unavailable'
            WHERE status='pending' AND item_key IN (SELECT item_key FROM blocked_delivery_items)""")
        if available == []:
            upper = 0
        else:
            source_clause = '' if available is None else (
                ' AND EXISTS(SELECT 1 FROM seen_items s WHERE s.item_key=d.item_key AND s.source_key IN ('+
                ','.join('?' for _ in available)+'))')
            upper = self._conn.execute("""SELECT COALESCE(MAX(d.id),0) FROM user_deliveries d
                WHERE d.status='suspended' AND NOT EXISTS
                    (SELECT 1 FROM blocked_delivery_items b WHERE b.item_key=d.item_key)"""+source_clause,
                () if available is None else available).fetchone()[0]
        self._put_flag('resume_cursor','0')
        self._put_flag('resume_upper',upper)
        self._put_flag('queue_availability',signature)
        self._put_flag('queue_dirty','0')

    def _resume_batch(self, slots, budget, available, blocked, stats):
        cursor,upper = int(self._flag('resume_cursor')),int(self._flag('resume_upper'))
        if not slots or not budget or cursor >= upper:
            return slots
        rows = self._conn.execute("""SELECT d.id,d.item_key,s.source_key,u.is_active
            FROM user_deliveries d INDEXED BY idx_delivery_status_id
            JOIN seen_items s ON s.item_key=d.item_key JOIN users u ON u.telegram_id=d.telegram_id
            WHERE d.status='suspended' AND d.id>? AND d.id<=? ORDER BY d.id LIMIT ?""",(cursor,upper,budget)).fetchall()
        for row in rows:
            if not slots:
                break
            cursor = row['id']
            stats['suspended_examined'] += 1
            if (row['is_active'] and row['item_key'] not in blocked
                    and (available is None or row['source_key'] in available)):
                self._conn.execute("UPDATE user_deliveries SET status='pending',reason='source_recovered' WHERE id=?",(row['id'],))
                slots -= 1
                stats['resumed'] += 1
        if not rows or (len(rows) < budget and cursor == rows[-1]['id']):
            cursor = upper
        self._put_flag('resume_cursor',cursor)
        return slots

    def _fanout_batch(self, slots, budget, sources, stats):
        if not slots or not budget or not sources:
            return slots
        last = self._flag('queue_source_cursor','')
        sources = [key for key in sources if key > last]+[key for key in sources if key <= last]
        quota = max(1,budget//len(sources))
        users_left,jobs_left = budget,budget
        for source in sources:
            source_users = min(quota,users_left)
            source_jobs = min(quota,jobs_left)
            visited = False
            while slots and source_users and source_jobs:
                job = self._conn.execute("""SELECT f.*,s.delivery_state,s.eligible_at,s.opportunity_at
                    FROM delivery_fanout f JOIN seen_items s ON s.item_key=f.item_key
                    WHERE f.source_key=? AND NOT EXISTS
                        (SELECT 1 FROM blocked_delivery_items b WHERE b.item_key=f.item_key)
                    ORDER BY f.priority,f.id LIMIT 1""",(source,)).fetchone()
                if not job:
                    break
                visited = True
                stats['jobs_examined'] += 1
                source_jobs -= 1; jobs_left -= 1
                if job['delivery_state'] != 'pending' or job['eligible_at'] is None:
                    self._conn.execute('DELETE FROM delivery_fanout WHERE id=?',(job['id'],))
                    continue
                # Seek by registry PK, not by history or filtered subscriber count.
                # Ineligible and already-materialized users also advance the cursor.
                page_limit = source_users
                rows = self._conn.execute("""SELECT telegram_id,
                    (is_active=1 AND subscribed_at<=? AND (? IS NULL OR
                        julianday(?)>julianday(subscribed_at,'unixepoch'))) AS eligible
                    FROM users WHERE telegram_id>? AND telegram_id<=? ORDER BY telegram_id LIMIT ?""",
                    (job['eligible_at'],job['opportunity_at'],job['opportunity_at'],
                     job['cursor_id'],job['audience_max_id'],page_limit)).fetchall()
                cursor = job['cursor_id']
                for user in rows:
                    if not slots:
                        break
                    cursor = user['telegram_id']
                    stats['users_examined'] += 1
                    source_users -= 1; users_left -= 1
                    if user['eligible']:
                        inserted = self._conn.execute("""INSERT OR IGNORE INTO user_deliveries
                            (telegram_id,item_key,status) VALUES(?,?,'pending')""",(cursor,job['item_key'])).rowcount
                        slots -= inserted
                        stats['created'] += inserted
                # Do not skip the unprocessed tail when capacity ends mid-page.
                complete = (cursor >= job['audience_max_id'] or not rows or
                            (len(rows) < page_limit and cursor == rows[-1]['telegram_id']))
                if complete:
                    self._conn.execute('DELETE FROM delivery_fanout WHERE id=?',(job['id'],))
                else:
                    self._conn.execute('UPDATE delivery_fanout SET cursor_id=? WHERE id=?',(cursor,job['id']))
            if visited:
                self._put_flag('queue_source_cursor',source)
            if not slots or not users_left or not jobs_left:
                break
        return slots

    def fill_user_queue(self, *, capacity=10000, batch=500,
                        available_sources=None, configured_sources=None, blocked_items=()):
        """Bounded durable audience pagination plus bounded suspended recovery."""
        stats = dict(users_examined=0,suspended_examined=0,jobs_examined=0,created=0,resumed=0)
        with self._lock, self._conn:
            configured = None if configured_sources is None else sorted(configured_sources)
            available = None if available_sources is None else sorted(available_sources)
            if configured is not None and available is not None:
                available = [key for key in available if key in configured]
            elif configured is not None:
                available = configured
            blocked = set(blocked_items)
            # TEMP tables do not survive a restart even when the durable signature does.
            self._conn.execute('DELETE FROM blocked_delivery_items')
            self._conn.executemany('INSERT INTO blocked_delivery_items VALUES(?)',((key,) for key in blocked))
            self._reconcile_queue(configured,available,blocked)
            count = self._conn.execute("SELECT COUNT(*) FROM user_deliveries WHERE status IN ('pending','sending')").fetchone()[0]
            slots,budget = max(0,capacity-count),max(0,batch)
            if not slots or not budget:
                return stats
            sources = (available if available is not None else
                       [row[0] for row in self._conn.execute('SELECT DISTINCT source_key FROM delivery_fanout')])
            resume = int(self._flag('resume_cursor')) < int(self._flag('resume_upper'))
            fanout = bool(sources) and bool(self._conn.execute('SELECT 1 FROM delivery_fanout LIMIT 1').fetchone())
            first = 'fanout' if self._flag('queue_fill_kind') == 'resume' else 'resume'
            if not resume: first = 'fanout'
            if not fanout: first = 'resume'
            portion = max(1,budget//2) if resume and fanout else budget
            order = (first,'resume' if first == 'fanout' else 'fanout')
            for index,kind in enumerate(order):
                remaining = budget-stats['users_examined']-stats['suspended_examined']
                allowance = min(portion if index == 0 else remaining,remaining)
                before = stats['users_examined']+stats['suspended_examined']+stats['jobs_examined']
                if kind == 'fanout':
                    slots = self._fanout_batch(slots,allowance,sources,stats)
                else:
                    slots = self._resume_batch(slots,allowance,available,blocked,stats)
                if stats['users_examined']+stats['suspended_examined']+stats['jobs_examined'] > before:
                    self._put_flag('queue_fill_kind',kind)
                if not slots:
                    break
            return stats

    def user_pending(self, now, limit=100, after_id=0):
        with self._lock:
            return self._conn.execute("""SELECT s.*,d.id AS delivery_id,d.telegram_id,
                d.attempts AS user_attempts FROM user_deliveries d
                JOIN users u ON u.telegram_id=d.telegram_id JOIN seen_items s ON s.item_key=d.item_key
                WHERE d.status='pending' AND d.available_at<=? AND u.is_active=1 AND d.id>?
                ORDER BY d.id LIMIT ?""", (now.timestamp(),after_id,limit)).fetchall()

    def user_delivery(self, telegram_id, item_key):
        with self._lock:
            return self._conn.execute("SELECT * FROM user_deliveries WHERE telegram_id=? AND item_key=?", (telegram_id,item_key)).fetchone()

    def claim_user_delivery(self, delivery_id, now):
        with self._lock, self._conn:
            return self._conn.execute("""UPDATE user_deliveries SET status='sending',attempts=attempts+1,
                reason='request_started' WHERE id=? AND status='pending' AND available_at<=?
                AND EXISTS(SELECT 1 FROM users u WHERE u.telegram_id=user_deliveries.telegram_id AND u.is_active=1)""",
                (delivery_id,now.timestamp())).rowcount == 1

    def finish_user_delivery(self, delivery_id, state, reason, *, available_at=0, message_id=None):
        if state not in {'pending','sent','uncertain','failed','expired','skipped'}:
            raise ValueError('Invalid delivery state')
        with self._lock, self._conn:
            self._conn.execute("""UPDATE user_deliveries SET status=?,reason=?,available_at=?,
                telegram_message_id=COALESCE(?,telegram_message_id),sent_at=CASE WHEN ?='sent' THEN ? ELSE sent_at END
                WHERE id=?""", (state,reason,available_at,message_id,state,time.time(),delivery_id))
            if state == 'pending':
                self._put_flag('queue_dirty','1')

    def user_delivery_counts(self, telegram_id):
        with self._lock:
            return dict(self._conn.execute("SELECT status,COUNT(*) FROM user_deliveries WHERE telegram_id=? GROUP BY status", (telegram_id,)))

    def has_personal_delivery(self, item_key):
        with self._lock:
            return self._conn.execute("SELECT 1 FROM user_deliveries WHERE item_key=?", (item_key,)).fetchone() is not None

    def multi_delivery_counts(self, telegram_id=None):
        with self._lock:
            if telegram_id is None:
                return dict(self._conn.execute("SELECT status,COUNT(*) FROM user_deliveries GROUP BY status"))
            return dict(self._conn.execute("SELECT status,COUNT(*) FROM user_deliveries WHERE telegram_id=? GROUP BY status", (telegram_id,)))

    def multi_review(self, telegram_id=None):
        with self._lock:
            return self._conn.execute("""SELECT telegram_id,item_key,status,reason,telegram_message_id
                FROM user_deliveries WHERE status IN ('uncertain','failed','legacy_review')
                AND (? IS NULL OR telegram_id=?) ORDER BY id LIMIT 1000""", (telegram_id,telegram_id)).fetchall()

    def resolve_user_delivery(self, telegram_id, item_key, outcome, allow_duplicate_risk=False):
        mapping = {'sent':'sent','retry':'pending','skip':'skipped'}
        with self._lock, self._conn:
            row = self._conn.execute("SELECT status FROM user_deliveries WHERE telegram_id=? AND item_key=?", (telegram_id,item_key)).fetchone()
            if not row or row[0] not in {'uncertain','failed','legacy_review'} or outcome not in mapping:
                raise ValueError('Only uncertain, failed or legacy_review deliveries can be resolved')
            if outcome == 'retry':
                active = self._conn.execute("SELECT is_active FROM users WHERE telegram_id=?", (telegram_id,)).fetchone()
                if not active or not active[0]:
                    raise ValueError('Subscriber is inactive')
                if row[0] in {'uncertain','legacy_review'} and not allow_duplicate_risk:
                    raise ValueError('Check Telegram first; retry requires --allow-duplicate-risk')
            self._conn.execute("""UPDATE user_deliveries SET status=?,reason='operator_resolution',available_at=0,
                sent_at=CASE WHEN ?='sent' THEN ? ELSE sent_at END WHERE telegram_id=? AND item_key=?""",
                (mapping[outcome],outcome,time.time(),telegram_id,item_key))
            if outcome == 'retry':
                self._put_flag('queue_dirty','1')

    @staticmethod
    def serialize(item):
        value = asdict(item)
        for name in ("published_at", "deadline", "source_deadline"):
            value[name] = value[name].isoformat() if value[name] else None
        return json.dumps(value, ensure_ascii=False)

    @staticmethod
    def deserialize(payload):
        value = json.loads(payload)
        for name in ("published_at", "deadline", "source_deadline"):
            if value.get(name):
                value[name] = datetime.fromisoformat(value[name])
        return Item(**value)

    def bind_identity(self, name, value):
        with self._lock, self._conn:
            row = self._conn.execute("SELECT value FROM flags WHERE name=?", (name,)).fetchone()
            if row and row[0] != str(value):
                raise RuntimeError("Database belongs to a different owner or bot; use a separate DB_PATH")
            self._conn.execute("INSERT OR IGNORE INTO flags(name,value) VALUES(?,?)", (name,str(value)))

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    def exists(self, item_key: str) -> bool:
        with self._lock:
            cur = self._conn.execute("SELECT 1 FROM seen_items WHERE item_key=?", (item_key,))
            return cur.fetchone() is not None

    def title_hash_seen(self, title_hash: str) -> bool:
        """Появилось ли уже такое объявление из ДРУГОГО источника (дедуп канал<->сайт)."""
        if not title_hash:
            return False
        with self._lock:
            cur = self._conn.execute(
                "SELECT 1 FROM seen_items WHERE title_hash=? LIMIT 1", (title_hash,)
            )
            return cur.fetchone() is not None

    def is_duplicate(self, item, *, now=None):
        cutoff = (now or datetime.now(timezone.utc)).timestamp() - 30 * 86400
        with self._lock:
            for alias in identities(item):
                rows = self._conn.execute("""SELECT s.payload, COALESCE(s.content_updated_at,s.first_seen_at) AS identity_at FROM item_aliases a
                    JOIN seen_items s ON a.item_key=s.item_key WHERE a.alias=? AND s.item_key<>?
                    AND (s.delivery_state IN ('pending','sending','sent','uncertain','baseline','legacy_review')
                    OR EXISTS(SELECT 1 FROM user_deliveries d WHERE d.item_key=s.item_key
                        AND d.status IN ('sending','sent','uncertain')))""",
                    (alias,item.item_key)).fetchall()
                for row in rows:
                    if datetime.fromisoformat(row['identity_at']).timestamp() < cutoff:
                        continue
                    other = self.deserialize(row['payload']) if row['payload'] else None
                    if other and other.deadline and item.deadline and other.deadline != item.deadline:
                        continue
                    return True
        return False

    def add_item(self, item, baseline=False, category=None, *, eligible_at=None, content_hash=None):
        state = ('baseline' if category else 'rejected') if baseline else 'pending'
        with self._lock, self._conn:
            inserted = self._conn.execute("""INSERT OR IGNORE INTO seen_items
                (item_key,source_key,external_id,url,title,summary,apply_url,category,title_hash,
                 published_at,first_seen_at,notified,baseline,payload,delivery_state)
                 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                item.item_key,item.source_key,item.external_id,item.url,item.title,item.summary,
                item.apply_url,category,item.title_hash,
                item.published_at.isoformat() if item.published_at else None,self._now(),
                int(baseline),int(baseline),self.serialize(item),state))
            if inserted.rowcount:
                self._conn.execute('UPDATE seen_items SET content_hash=?,opportunity_at=?,content_updated_at=? WHERE item_key=?',
                    (content_hash or item.content_hash,item.published_at.isoformat() if item.published_at else None,self._now(),item.item_key))
            if inserted.rowcount and not baseline and category:
                self._conn.execute("UPDATE seen_items SET eligible_at=? WHERE item_key=?",
                                   (eligible_at if eligible_at is not None else time.time(), item.item_key))
                self._schedule_fanout(item.item_key)
            if category:
                for alias in identities(item):
                    self._conn.execute("INSERT OR IGNORE INTO item_aliases VALUES(?,?)",(alias,item.item_key))

    def record_unread_details(self, source_key, catalog, failed_keys, *, baseline, eligible_at):
        """Remember first catalogue admission even when a detail is unavailable."""
        with self._lock, self._conn:
            for key in failed_keys:
                identifier,url = catalog[key]
                self._conn.execute("""INSERT OR IGNORE INTO seen_items
                    (item_key,source_key,external_id,url,first_seen_at,notified,baseline,delivery_state,eligible_at)
                    VALUES(?,?,?,?,?,?,?,?,?)""", (key,source_key,identifier,url,self._now(),int(baseline),int(baseline),
                    'baseline' if baseline else 'awaiting_detail',None if baseline else eligible_at))

    def revise_item(self, item, content_hash, *, state, activate_at=None, first_detail=False):
        """Update content without replaying sent/uncertain/skipped deliveries."""
        with self._lock, self._conn:
            self._conn.execute("""UPDATE seen_items SET title=?,summary=?,url=?,apply_url=?,
                published_at=?,title_hash=?,category=?,payload=?,content_hash=?,revision=revision+1,
                delivery_state=?,content_updated_at=? WHERE item_key=?""", (item.title,item.summary,item.url,item.apply_url,
                item.published_at.isoformat() if item.published_at else None,item.title_hash,
                item.categories[0] if item.categories else None,self.serialize(item),content_hash,state,self._now(),item.item_key))
            self._conn.execute('DELETE FROM item_aliases WHERE item_key=?',(item.item_key,))
            delivered = self._conn.execute("SELECT 1 FROM user_deliveries WHERE item_key=? AND status IN ('sending','sent','uncertain') LIMIT 1", (item.item_key,)).fetchone()
            if (item.categories and state != 'rejected') or delivered:
                self._conn.executemany('INSERT OR IGNORE INTO item_aliases VALUES(?,?)',
                                       ((alias,item.item_key) for alias in identities(item)))
            if first_detail:
                self._conn.execute('UPDATE seen_items SET opportunity_at=published_at WHERE item_key=?',(item.item_key,))
            if state == 'baseline':
                self._conn.execute('UPDATE seen_items SET baseline=1,notified=1 WHERE item_key=?',(item.item_key,))
            if state in {'rejected','baseline'}:
                self._conn.execute('UPDATE seen_items SET eligible_at=NULL WHERE item_key=?',(item.item_key,))
            if state == 'rejected':
                self._conn.execute("""UPDATE user_deliveries SET status='expired',reason='publication_changed'
                    WHERE item_key=? AND status IN ('pending','suspended')""",(item.item_key,))
            if activate_at is not None:
                observed = datetime.fromtimestamp(activate_at,timezone.utc).isoformat()
                self._conn.execute("""UPDATE seen_items SET eligible_at=?,opportunity_at=?,baseline=0,notified=0
                    WHERE item_key=?""",(activate_at,observed,item.item_key))
                reopened = self._conn.execute("""UPDATE user_deliveries SET status='suspended',reason='publication_reopened'
                    WHERE item_key=? AND status='expired' AND telegram_id IN
                    (SELECT telegram_id FROM users WHERE is_active=1 AND subscribed_at<=?)""",(item.item_key,activate_at))
                if reopened.rowcount:
                    self._put_flag('queue_dirty','1')
            if state != 'pending':
                self._conn.execute('DELETE FROM delivery_fanout WHERE item_key=?',(item.item_key,))
            elif activate_at is not None or first_detail:
                self._schedule_fanout(item.item_key,reset=True)

    def item_row(self, item_key):
        with self._lock:
            return self._conn.execute("SELECT * FROM seen_items WHERE item_key=?",(item_key,)).fetchone()

    def refresh_pending(self, item):
        with self._lock, self._conn:
            self._conn.execute("UPDATE seen_items SET payload=? WHERE item_key=? AND delivery_state='pending'",
                               (self.serialize(item),item.item_key))

    def claim_delivery(self, item_key, now):
        with self._lock, self._conn:
            cursor = self._conn.execute("""UPDATE seen_items SET delivery_state='sending',
                delivery_reason='request_started',attempts=attempts+1
                WHERE item_key=? AND delivery_state='pending' AND available_at<=?""",(item_key,now.timestamp()))
            return cursor.rowcount == 1

    def set_delivery(self, item_key, state, reason, *, available_at=0, message_id=None):
        if state not in {'pending','sent','uncertain','failed','expired','skipped'}:
            raise ValueError('Invalid delivery state')
        with self._lock, self._conn:
            self._conn.execute("""UPDATE seen_items SET delivery_state=?,delivery_reason=?,
                available_at=?,message_id=COALESCE(?,message_id),notified=CASE WHEN ?='sent' THEN 1 ELSE notified END
                WHERE item_key=?""",(state,reason,available_at,message_id,state,item_key))
            if state == 'pending':
                self._schedule_fanout(item_key)
            else:
                self._conn.execute('DELETE FROM delivery_fanout WHERE item_key=?',(item_key,))

    def mark_notified(self, item_key):
        self.set_delivery(item_key,'sent','delivered')

    def pending_unnotified(self, now=None):
        timestamp = (now or datetime.now(timezone.utc)).timestamp()
        with self._lock:
            return self._conn.execute("""SELECT * FROM seen_items WHERE delivery_state='pending'
                AND available_at<=? ORDER BY first_seen_at,item_key""",(timestamp,)).fetchall()

    def delivery_counts(self):
        with self._lock:
            return dict(self._conn.execute("SELECT delivery_state,COUNT(*) FROM seen_items GROUP BY delivery_state"))

    def review_deliveries(self):
        with self._lock:
            return self._conn.execute("SELECT item_key,delivery_state,delivery_reason,message_id FROM seen_items WHERE delivery_state IN ('uncertain','failed','legacy_review')").fetchall()

    def apply_control(self, enabled=None, offset=None, check_requested_at=None):
        """Persist command state and update offset atomically before acknowledging."""
        with self._lock, self._conn:
            values = []
            if enabled is not None:
                values.append(('enabled','1' if enabled else '0'))
                if enabled:
                    prior = dict(self._conn.execute("SELECT name,value FROM flags WHERE name IN ('enabled','ever_started','resume_generation')"))
                    if prior.get('enabled') != '1' and prior.get('ever_started') == '1':
                        values.append(('resume_generation',str(int(prior.get('resume_generation','0'))+1)))
                    values.append(('ever_started','1'))
                else:
                    self._conn.execute("UPDATE seen_items SET delivery_state='skipped',delivery_reason='paused' WHERE delivery_state='pending'")
            if offset is not None:
                values.append(('offset',str(offset)))
            if check_requested_at is not None:
                values.extend([('check_pending','1'),('check_last_request',str(check_requested_at))])
            self._conn.executemany("INSERT INTO flags(name,value) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value",values)

    def resolve_delivery(self, item_key, outcome):
        mapping = {'sent':'sent','retry':'pending','skip':'skipped'}
        if outcome not in mapping:
            raise ValueError('Invalid resolution')
        with self._lock, self._conn:
            row = self._conn.execute('SELECT delivery_state FROM seen_items WHERE item_key=?',(item_key,)).fetchone()
            if not row or row[0] not in {'uncertain','failed','legacy_review'}:
                raise ValueError('Only uncertain, failed or legacy_review items can be resolved')
            self._conn.execute("UPDATE seen_items SET delivery_state=?,delivery_reason='operator_resolution',available_at=0,notified=? WHERE item_key=?",
                               (mapping[outcome],int(outcome=='sent'),item_key))
            if outcome == 'retry':
                self._schedule_fanout(item_key)
            else:
                self._conn.execute('DELETE FROM delivery_fanout WHERE item_key=?',(item_key,))

    def count_items(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) c FROM seen_items").fetchone()["c"]

    def telegram_cursor(self, source_key: str) -> int | None:
        """Reuse legacy seen IDs on upgrade; never recreate or delete the DB."""
        saved = self.get_flag("telegram_cursor:" + source_key)
        if saved == "initializing":
            return None
        if saved is not None:
            value = int(saved)
            if value < 1:
                raise ValueError("Invalid Telegram cursor")
            return value
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(CAST(external_id AS INTEGER)) FROM seen_items WHERE source_key=? "
                "AND external_id <> '' AND external_id NOT GLOB '*[^0-9]*'",
                (source_key,),
            ).fetchone()
            return row[0] if row[0] and row[0] > 0 else None

    def save_telegram_cursor(self, source_key: str, value: int | None) -> None:
        # A first snapshot interrupted during ingestion must remain a baseline
        # on restart, even if some rows have already reached seen_items.
        if value is not None and (type(value) is not int or value < 1):
            raise ValueError("Invalid Telegram cursor")
        self.set_flag("telegram_cursor:" + source_key, str(value) if value is not None else "initializing")

    def accept_telegram_gap(self, source_key, through_id):
        """Offline operator action: record an explicit forward-only history gap."""
        with self._lock, self._conn:
            prior = self._flag('telegram_cursor:'+source_key,'initializing')
            if (prior == 'initializing' or type(through_id) is not int
                    or through_id <= int(prior)):
                raise ValueError('Укажите ID больше существующего сохранённого курсора')
            self._put_flag('telegram_cursor:'+source_key,through_id)
            self._put_flag('telegram_gap:'+source_key,json.dumps(
                dict(previous=int(prior),through=through_id,accepted_at=time.time())))

    def source_initialized(self, source_key: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT initialized FROM source_state WHERE source_key=?", (source_key,)
            ).fetchone()
            return bool(row and row["initialized"])

    def set_source_state(self, source_key: str, error: str | None = None, initialized: bool | None = None) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO source_state (source_key, last_check_at, last_error, initialized)
                   VALUES (?,?,?,COALESCE((SELECT initialized FROM source_state WHERE source_key=?),0))
                   ON CONFLICT(source_key) DO UPDATE SET
                     last_check_at=excluded.last_check_at,
                     last_error=CASE WHEN excluded.last_error IS NULL THEN source_state.last_error ELSE excluded.last_error END""",
                (source_key, self._now(), error, source_key),
            )
            if initialized is not None:
                self._conn.execute(
                    "UPDATE source_state SET initialized=? WHERE source_key=?",
                    (1 if initialized else 0, source_key),
                )
            self._conn.commit()

    def get_source_error(self, source_key: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT last_error FROM source_state WHERE source_key=?", (source_key,)
            ).fetchone()
            return row["last_error"] if row else None

    def source_states(self) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute("SELECT * FROM source_state ORDER BY source_key").fetchall()

    def get_flag(self, name: str, default: str | None = None) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT value FROM flags WHERE name=?", (name,)).fetchone()
            return row["value"] if row else default

    def set_flag(self, name: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO flags(name,value) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value",
                (name, value),
            )
            self._conn.commit()

    def log_run(self, started_at: str, finished_at: str, found: int, new: int, notified: int, errors: str | None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO runs VALUES (?,?,?,?,?,?)",
                (started_at, finished_at, found, new, notified, errors),
            )
            # храним последние 200 запусков
            self._conn.execute(
                """DELETE FROM runs WHERE rowid NOT IN
                   (SELECT rowid FROM runs ORDER BY rowid DESC LIMIT 200)"""
            )
            self._conn.commit()

    def last_run(self):
        with self._lock:
            return self._conn.execute("SELECT * FROM runs ORDER BY rowid DESC LIMIT 1").fetchone()

    def close(self):
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
            if self._file_lock is not None:
                self._file_lock.close()
                self._file_lock = None
