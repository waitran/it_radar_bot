"""Private Mini App storage; no ATTACH, foreign keys or access to bot queues."""
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from .storage import protect_catalog, storage_lock


class Favorites:
    def __init__(self, path, catalog_path):
        self.path = protect_catalog(path, catalog_path)
        self.uri = self.path.as_uri() + '?mode=rw'
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.close(fd)
            except FileExistsError:
                pass
            with storage_lock(self.path), self.connect() as conn:
                # Inspect before any mutating PRAGMA, even for an accidentally selected unrelated DB.
                existing = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if conn.execute('PRAGMA user_version').fetchone()[0] > 1 or existing - {'favorites'}:
                    raise ValueError('Unsupported Mini App user database')
                conn.execute('PRAGMA journal_mode=WAL')
                conn.execute('BEGIN IMMEDIATE')
                version = conn.execute('PRAGMA user_version').fetchone()[0]
                tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if version > 1 or tables - {'favorites'}:
                    raise ValueError('Unsupported Mini App user database')
                conn.execute('''CREATE TABLE IF NOT EXISTS favorites (
                    telegram_id INTEGER NOT NULL,
                    item_key TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (telegram_id, item_key)
                )''')
                info = conn.execute('PRAGMA table_info(favorites)').fetchall()
                columns = [r[1] for r in info]
                if columns != ['telegram_id', 'item_key', 'created_at'] or [r[5] for r in info] != [1, 2, 0] or not all(r[3] for r in info):
                    raise ValueError('Unsupported Mini App favorites schema')
                conn.execute('CREATE INDEX IF NOT EXISTS idx_favorites_user_created ON favorites(telegram_id,created_at DESC,item_key)')
                conn.execute('PRAGMA user_version=1')
                conn.commit()
        except (OSError, sqlite3.Error):
            raise RuntimeError('Mini App user storage unavailable') from None

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.uri, uri=True, timeout=2)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def health(self):
        with self.connect() as conn:
            conn.execute('SELECT item_key FROM favorites LIMIT 1').fetchone()

    def keys(self, uid, keys):
        if not keys:
            return set()
        placeholders = ','.join('?' for _ in keys)
        with self.connect() as conn:
            return {r[0] for r in conn.execute(
                f'SELECT item_key FROM favorites WHERE telegram_id=? AND item_key IN ({placeholders})', [uid, *keys])}

    def page(self, uid, page, page_size):
        with self.connect() as conn:
            conn.execute('BEGIN')
            total = conn.execute('SELECT count(*) FROM favorites WHERE telegram_id=?', (uid,)).fetchone()[0]
            rows = conn.execute('''SELECT item_key,created_at FROM favorites WHERE telegram_id=?
                ORDER BY created_at DESC,item_key ASC LIMIT ? OFFSET ?''', (uid, page_size, (page-1)*page_size)).fetchall()
            return [dict(row) for row in rows], total

    def save(self, uid, key):
        with self.connect() as conn, conn:
            conn.execute('INSERT INTO favorites VALUES(?,?,?) ON CONFLICT(telegram_id,item_key) DO NOTHING',
                         (uid, key, datetime.now(timezone.utc).isoformat()))

    def remove(self, uid, key):
        with self.connect() as conn, conn:
            conn.execute('DELETE FROM favorites WHERE telegram_id=? AND item_key=?', (uid, key))
