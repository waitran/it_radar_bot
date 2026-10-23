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
            existing = {row[1] for row in self._conn.execute("PRAGMA table_info(seen_items)")}
            if existing and "delivery_state" not in existing and path != ":memory:":
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
                }.items():
                    if name not in columns:
                        self._conn.execute(f"ALTER TABLE seen_items ADD COLUMN {name} {definition}")
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
                for row in self._conn.execute("SELECT * FROM seen_items WHERE payload IS NULL AND category IS NOT NULL").fetchall():
                    old = Item(row['source_key'],row['source_key'],row['external_id'],row['url'] or '',
                               row['title'] or '',row['summary'] or '',row['apply_url'],None)
                    for alias in identities(old):
                        self._conn.execute("INSERT OR IGNORE INTO item_aliases VALUES(?,?)",(alias,row['item_key']))
                self._conn.execute("PRAGMA user_version=1")
        except Exception:
            self.close()
            raise

    @staticmethod
    def serialize(item):
        value = asdict(item)
        for name in ("published_at", "deadline"):
            value[name] = value[name].isoformat() if value[name] else None
        return json.dumps(value, ensure_ascii=False)

    @staticmethod
    def deserialize(payload):
        value = json.loads(payload)
        for name in ("published_at", "deadline"):
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
                rows = self._conn.execute("""SELECT s.payload, s.first_seen_at FROM item_aliases a
                    JOIN seen_items s ON a.item_key=s.item_key WHERE a.alias=? AND s.item_key<>?
                    AND s.delivery_state IN ('pending','sending','sent','uncertain','baseline','legacy_review')""",
                    (alias,item.item_key)).fetchall()
                for row in rows:
                    if datetime.fromisoformat(row['first_seen_at']).timestamp() < cutoff:
                        continue
                    other = self.deserialize(row['payload']) if row['payload'] else None
                    if other and other.deadline and item.deadline and other.deadline != item.deadline:
                        continue
                    return True
        return False

    def add_item(self, item, baseline=False, category=None):
        state = ('baseline' if category else 'rejected') if baseline else 'pending'
        with self._lock, self._conn:
            self._conn.execute("""INSERT OR IGNORE INTO seen_items
                (item_key,source_key,external_id,url,title,summary,apply_url,category,title_hash,
                 published_at,first_seen_at,notified,baseline,payload,delivery_state)
                 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                item.item_key,item.source_key,item.external_id,item.url,item.title,item.summary,
                item.apply_url,category,item.title_hash,
                item.published_at.isoformat() if item.published_at else None,self._now(),
                int(baseline),int(baseline),self.serialize(item),state))
            if category:
                for alias in identities(item):
                    self._conn.execute("INSERT OR IGNORE INTO item_aliases VALUES(?,?)",(alias,item.item_key))

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

    def apply_control(self, enabled=None, offset=None):
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
