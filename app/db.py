"""SQLite: хранилище просмотренных публикаций, состояний источников и статуса доставки."""
from __future__ import annotations

import sqlite3
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
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

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

    def add_item(self, item, baseline: bool = False, category: str | None = None) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT OR IGNORE INTO seen_items
                   (item_key, source_key, external_id, url, title, summary, apply_url,
                    category, title_hash, published_at, first_seen_at, notified, baseline)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    item.item_key, item.source_key, item.external_id, item.url,
                    item.title, item.summary, item.apply_url, category,
                    item.title_hash,
                    item.published_at.isoformat(timespec="seconds") if item.published_at else None,
                    self._now(), 1 if baseline else 0, 1 if baseline else 0,
                ),
            )
            self._conn.commit()

    def mark_notified(self, item_key: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE seen_items SET notified=1 WHERE item_key=?", (item_key,))
            self._conn.commit()

    def pending_unnotified(self) -> list[sqlite3.Row]:
        """Не отправленные владельцу записи (ретрайд после рестарта)."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM seen_items WHERE notified=0 AND baseline=0 ORDER BY first_seen_at"
            )
            return cur.fetchall()

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

    def close(self) -> None:
        with self._lock:
            self._conn.close()
