"""Independent read-only reader. Never imports or constructs the bot Database."""
import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

CATEGORIES = ('internships', 'events', 'contests', 'career')


def categories(payload, category):
    try:
        values = json.loads(payload or '{}').get('categories', [])
    except (ValueError, AttributeError):
        values = []
    if not isinstance(values, list):
        values = []
    return [c for c in CATEGORIES if c in values or c == category]


def public_url(value):
    try:
        if not value or len(value) > 2048 or '\\' in value or any(c.isspace() or ord(c) < 32 for c in value):
            return None
        url = urlsplit(value)
        _ = url.port
        return value if url.scheme in ('http', 'https') and url.hostname and not url.username and not url.password else None
    except ValueError:
        return None


class Catalog:
    def __init__(self, path):
        self.uri = Path(path).absolute().as_uri() + '?mode=ro'

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.uri, uri=True, timeout=2)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA query_only=ON')
            # Bound expensive scans; do not hold a read snapshot indefinitely.
            until = time.monotonic() + 2
            conn.set_progress_handler(lambda: int(time.monotonic() > until), 1000)
            conn.create_function('public_categories', 2, lambda p, c: json.dumps(categories(p, c)))
            conn.create_function('has_category', 3, lambda p, c, q: int(q in categories(p, c)))
            yield conn
        finally:
            conn.close()

    def health(self):
        with self.connect() as conn:
            conn.execute('SELECT item_key,length(payload) FROM seen_items LIMIT 1').fetchone()

    def select(self, *, q='', category=None, active_only=False, page=1, page_size=20, sort='desc', key=None, keys=None):
        with self.connect() as conn:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            indexed = {'event_metadata', 'item_refs'} <= tables
            metadata = ('m.event_at,m.deadline_at,m.event_precision,m.deadline_precision,m.registration_status'
                        if indexed else "NULL event_at,NULL deadline_at,NULL event_precision,NULL deadline_precision,'unknown' registration_status")
            base = ' FROM seen_items s '
            if indexed:
                base += 'LEFT JOIN item_refs r ON r.item_key=s.item_key LEFT JOIN event_metadata m ON m.item_id=r.id '
            where = ["length(s.item_key) BETWEEN 1 AND 512", "s.category IN ('internships','events','contests','career')"]
            params = []
            if q:
                # instr is literal (including SQL wildcard characters) and Unicode aware via casefold.
                conn.create_function('fold', 1, lambda s: (s or '').casefold())
                where.append('(instr(fold(s.title),?)>0 OR instr(fold(s.summary),?)>0)')
                params += [q.casefold(), q.casefold()]
            if category:
                where.append('has_category(s.payload,s.category,?)=1')
                params.append(category)
            if key is not None:
                where.append('s.item_key=?')
                params.append(key)
            if keys is not None:
                if not keys:
                    where.append('0')
                else:
                    where.append('s.item_key IN (' + ','.join('?' for _ in keys) + ')')
                    params.extend(keys)
            if active_only:
                # Unknown dates/status do not prove an opportunity is still actionable.
                if indexed:
                    where.append("m.registration_status!='closed' AND (m.deadline_at IS NULL OR m.deadline_at>=?) AND (m.event_at IS NULL OR m.event_at>=?) AND (m.deadline_at IS NOT NULL OR m.event_at IS NOT NULL)")
                    params += [time.time(), time.time()]
                else:
                    where.append('0')
            base += ' WHERE ' + ' AND '.join(where)
            conn.execute('BEGIN')  # count and page share one consistent, short snapshot
            total = conn.execute('SELECT count(*)'+base, params).fetchone()[0]
            order = 'ASC' if sort == 'asc' else 'DESC'
            rows = conn.execute('SELECT s.item_key,substr(s.title,1,512) title,substr(s.summary,1,8000) summary,CASE WHEN length(s.url)<=2048 THEN s.url END url,CASE WHEN length(s.apply_url)<=2048 THEN s.apply_url END apply_url,public_categories(s.payload,s.category) categories,s.published_at,'+metadata+base+
                                f' ORDER BY julianday(s.published_at) IS NULL,julianday(s.published_at) {order},s.item_key ASC LIMIT ? OFFSET ?',
                                params+[page_size, (page-1)*page_size]).fetchall()
            items = []
            for row in rows:
                item = dict(row)
                item['id'] = item.pop('item_key')
                item['categories'] = json.loads(item['categories'])
                for name in ('url', 'apply_url'):
                    item[name] = public_url(item[name])
                for name in ('title', 'summary'):
                    item[name] = item[name] or ''
                items.append(item)
            return dict(items=items,total=total,page=page,page_size=page_size)
