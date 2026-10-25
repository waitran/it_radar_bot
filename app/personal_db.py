"""SQLite extension; private helpers run under Database's existing lock/transaction."""
import json
import time
from copy import deepcopy
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from app.preferences import DEFAULTS, CATEGORIES, TOPICS, matches
from app.event_index import extract
from app.reminders import due_at

DDL = [
    "CREATE TABLE IF NOT EXISTS user_preferences(telegram_id INTEGER PRIMARY KEY, payload TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS item_refs(id INTEGER PRIMARY KEY AUTOINCREMENT, item_key TEXT UNIQUE NOT NULL REFERENCES seen_items(item_key))",
    """CREATE TABLE IF NOT EXISTS event_metadata(item_id INTEGER PRIMARY KEY REFERENCES item_refs(id),
       event_at REAL,event_day TEXT,event_precision TEXT,deadline_at REAL,deadline_day TEXT,
       deadline_precision TEXT,topics TEXT NOT NULL,format TEXT NOT NULL,city TEXT NOT NULL,registration_status TEXT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS idx_agenda ON event_metadata(event_at,item_id)",
    "CREATE INDEX IF NOT EXISTS idx_agenda_day ON event_metadata(event_day,item_id)",
    """CREATE TABLE IF NOT EXISTS saved_items(telegram_id INTEGER NOT NULL,item_id INTEGER NOT NULL REFERENCES item_refs(id),
       saved_at REAL NOT NULL, PRIMARY KEY(telegram_id,item_id))""",
    "CREATE INDEX IF NOT EXISTS idx_saved_page ON saved_items(telegram_id,saved_at,item_id)",
    """CREATE TABLE IF NOT EXISTS reminder_jobs(id INTEGER PRIMARY KEY AUTOINCREMENT,telegram_id INTEGER NOT NULL,item_id INTEGER NOT NULL,
       days INTEGER NOT NULL CHECK(days IN (1,3)),due_at REAL NOT NULL,status TEXT NOT NULL DEFAULT 'pending',
       available_at REAL NOT NULL DEFAULT 0, message_id INTEGER, cancel_requested INTEGER NOT NULL DEFAULT 0, UNIQUE(telegram_id,item_id,days),
       FOREIGN KEY(item_id) REFERENCES item_refs(id))""",
    "CREATE INDEX IF NOT EXISTS idx_reminder_due ON reminder_jobs(status,due_at,available_at)",
    "CREATE INDEX IF NOT EXISTS idx_reminder_user ON reminder_jobs(telegram_id,status,due_at,id)",
    "CREATE INDEX IF NOT EXISTS idx_reminder_item ON reminder_jobs(item_id,status,telegram_id)",
]


class PersonalDatabase:
    def _migrate_personal(self):
        for sql in DDL: self._conn.execute(sql)
        if 'reply_markup' not in {r[1] for r in self._conn.execute('PRAGMA table_info(command_replies)')}:
            self._conn.execute('ALTER TABLE command_replies ADD COLUMN reply_markup TEXT')
        if 'cancel_requested' not in {r[1] for r in self._conn.execute('PRAGMA table_info(reminder_jobs)')}:
            self._conn.execute('ALTER TABLE reminder_jobs ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0')
        self._conn.execute("UPDATE reminder_jobs SET status='uncertain' WHERE status='sending'")
        self._conn.execute('INSERT OR IGNORE INTO user_preferences SELECT telegram_id,? FROM users',(json.dumps(DEFAULTS),))
        if self._flag('event_index_backfilled', '') != '1':
            for row in self._conn.execute('SELECT item_key,payload,category FROM seen_items WHERE payload IS NOT NULL'):
                item = self.deserialize(row['payload'])
                if not item.categories and row['category']: item.categories = [row['category']]
                self._index_item(item)
            self._put_flag('event_index_backfilled', '1')

    def _preferences(self, uid):
        row = self._conn.execute('SELECT payload FROM user_preferences WHERE telegram_id=?',(uid,)).fetchone()
        return json.loads(row[0]) if row else deepcopy(DEFAULTS)

    def preferences(self, uid):
        with self._lock: return self._preferences(uid)

    def set_preferences(self, uid, preferences):
        if type(uid) is not int or uid <= 0: raise ValueError('Invalid sender')
        if set(preferences) != set(DEFAULTS): raise ValueError('Invalid preferences')
        if (not isinstance(preferences['categories'],list) or not isinstance(preferences['topics'],list)
            or set(preferences['categories'])-CATEGORIES.keys() or set(preferences['topics'])-TOPICS.keys()
            or preferences['format'] not in ('any','online','offline')
            or not isinstance(preferences['city'],str) or len(preferences['city'])>100):
            raise ValueError('Invalid preferences')
        ZoneInfo(preferences['timezone'])
        with self._lock, self._conn:
            self._conn.execute('INSERT INTO user_preferences VALUES(?,?) ON CONFLICT(telegram_id) DO UPDATE SET payload=excluded.payload',
                               (uid,json.dumps(preferences)))
            for row in self._conn.execute("SELECT DISTINCT item_id FROM reminder_jobs WHERE telegram_id=? AND status='pending'",(uid,)):
                self._reschedule(uid,row[0])

    def _personal_matches(self, uid, key):
        prefs = self._preferences(uid)
        if (set(prefs['categories'])==set(CATEGORIES) and not prefs['topics']
                and prefs['format']=='any' and not prefs['city']):
            return True
        row = self._conn.execute('SELECT payload,category FROM seen_items WHERE item_key=?',(key,)).fetchone()
        if not row or not row['payload']: return False
        item = self.deserialize(row['payload'])
        if not item.categories and row['category']: item.categories=[row['category']]
        return matches(prefs,item)

    def _index_item(self, item):
        self._conn.execute('INSERT OR IGNORE INTO item_refs(item_key) VALUES(?)',(item.item_key,))
        ref = self._conn.execute('SELECT id FROM item_refs WHERE item_key=?',(item.item_key,)).fetchone()[0]
        metadata = extract(item, self.metadata_timezone)
        columns = list(metadata)
        self._conn.execute('INSERT INTO event_metadata(item_id,'+','.join(columns)+') VALUES('+','.join('?' for _ in range(len(columns)+1))+') ON CONFLICT(item_id) DO UPDATE SET '+','.join(c+'=excluded.'+c for c in columns),
                           [ref]+[metadata[c] for c in columns])
        for row in self._conn.execute("SELECT DISTINCT telegram_id FROM reminder_jobs WHERE item_id=? AND status='pending'",(ref,)):
            self._reschedule(row[0],ref)
        return ref

    def item_ref(self, key):
        with self._lock:
            row=self._conn.execute('SELECT id FROM item_refs WHERE item_key=?',(key,)).fetchone()
            return row[0] if row else None

    def save_item(self, uid, ref):
        with self._lock, self._conn:
            row=self._conn.execute('SELECT s.* FROM item_refs r JOIN seen_items s ON s.item_key=r.item_key WHERE r.id=?',(ref,)).fetchone()
            if not row or not row['category'] or not self._personal_matches(uid,row['item_key']): return False
            self._conn.execute('INSERT OR IGNORE INTO saved_items VALUES(?,?,?)',(uid,ref,time.time()))
            return True

    def remove_saved(self, uid, ref):
        with self._lock, self._conn:
            # Receipts survive unsave/re-save; only pending work is cancelled.
            self._conn.execute("UPDATE reminder_jobs SET status='cancelled' WHERE telegram_id=? AND item_id=? AND status='pending'",(uid,ref))
            self._conn.execute('DELETE FROM saved_items WHERE telegram_id=? AND item_id=?',(uid,ref))

    def _reschedule(self, uid, ref):
        metadata=self._conn.execute('SELECT * FROM event_metadata WHERE item_id=?',(ref,)).fetchone()
        if not metadata: return
        prefs=self._preferences(uid)
        for job in self._conn.execute("SELECT * FROM reminder_jobs WHERE telegram_id=? AND item_id=? AND status='pending'",(uid,ref)):
            due=due_at(metadata,job['days'],prefs['timezone'])
            if due is None or due<=time.time() or metadata['deadline_at']<=time.time() or metadata['registration_status']=='closed':
                self._conn.execute("UPDATE reminder_jobs SET status='cancelled' WHERE id=?",(job['id'],))
            else: self._conn.execute('UPDATE reminder_jobs SET due_at=?,available_at=0 WHERE id=?',(due,job['id']))

    def enable_reminder(self, uid, ref, days):
        if days not in (1,3): return False
        with self._lock, self._conn:
            saved=self._conn.execute('SELECT 1 FROM saved_items WHERE telegram_id=? AND item_id=?',(uid,ref)).fetchone()
            user=self._conn.execute('SELECT is_active FROM users WHERE telegram_id=?',(uid,)).fetchone()
            metadata=self._conn.execute('SELECT * FROM event_metadata WHERE item_id=?',(ref,)).fetchone()
            if not saved or not user or not user[0] or not metadata or metadata['registration_status']=='closed': return False
            due=due_at(metadata,days,self._preferences(uid)['timezone'])
            if due is None or due<=time.time(): return False
            existing=self._conn.execute('SELECT status FROM reminder_jobs WHERE telegram_id=? AND item_id=? AND days=?',(uid,ref,days)).fetchone()
            if existing and existing[0] in ('sent','uncertain','failed','sending'):return False
            self._conn.execute("""INSERT INTO reminder_jobs(telegram_id,item_id,days,due_at) VALUES(?,?,?,?)
                ON CONFLICT(telegram_id,item_id,days) DO UPDATE SET status='pending',due_at=excluded.due_at,available_at=0,cancel_requested=0
                WHERE reminder_jobs.status='cancelled'""",(uid,ref,days,due))
            return True

    def cancel_reminder(self, uid, job):
        with self._lock, self._conn:
            self._conn.execute("UPDATE reminder_jobs SET cancel_requested=1 WHERE id=? AND telegram_id=? AND status='sending'",(job,uid))
            self._conn.execute("UPDATE reminder_jobs SET status='cancelled' WHERE id=? AND telegram_id=? AND status='pending'",(job,uid))

    def personal_page(self, uid, kind, page=0, now=None):
        page=max(0,min(int(page),10000)); now=now or datetime.now().astimezone()
        with self._lock:
            base='SELECT r.id AS ref,s.*,m.* FROM item_refs r JOIN seen_items s ON s.item_key=r.item_key JOIN event_metadata m ON m.item_id=r.id '
            if kind=='saved':
                sql=base+'JOIN saved_items v ON v.item_id=r.id WHERE v.telegram_id=? ORDER BY v.saved_at DESC,r.id DESC LIMIT 4 OFFSET ?'
                return self._conn.execute(sql,(uid,page*3)).fetchall()
            tz=ZoneInfo(self._preferences(uid)['timezone']); local=now.astimezone(tz)
            start=datetime.combine(local.date(),datetime.min.time(),tz)
            end=start+timedelta(days=7)
            # Range index bounds candidates; SQL paginates after personal relevance.
            prefs=self._preferences(uid)
            def relevant(payload,category):
                if not payload: return 0
                item=self.deserialize(payload)
                if not item.categories and category:item.categories=[category]
                return int(matches(prefs,item))
            self._conn.create_function('personal_relevant',2,relevant)
            sql=base+"WHERE ((m.event_precision='date' AND m.event_day>=? AND m.event_day<?) OR (m.event_precision='time' AND m.event_at>=? AND m.event_at<?)) AND s.category IS NOT NULL AND m.registration_status<>'closed' AND personal_relevant(s.payload,s.category)=1 ORDER BY m.event_at,r.id LIMIT 4 OFFSET ?"
            return self._conn.execute(sql,(start.date().isoformat(),end.date().isoformat(),now.timestamp(),end.timestamp(),page*3)).fetchall()

    def reminders_page(self, uid, page=0):
        with self._lock:
            return self._conn.execute("""SELECT j.*,s.title FROM reminder_jobs j JOIN item_refs r ON r.id=j.item_id
                JOIN seen_items s ON s.item_key=r.item_key WHERE j.telegram_id=? AND j.status='pending'
                ORDER BY j.due_at,j.id LIMIT 4 OFFSET ?""",(uid,max(0,min(page,10000))*3)).fetchall()

    def claim_reminder(self, now):
        with self._lock, self._conn:
            self._conn.execute("""UPDATE reminder_jobs SET status='cancelled' WHERE status='pending' AND
                (NOT EXISTS(SELECT 1 FROM saved_items v WHERE v.telegram_id=reminder_jobs.telegram_id AND v.item_id=reminder_jobs.item_id)
                 OR NOT EXISTS(SELECT 1 FROM users u WHERE u.telegram_id=reminder_jobs.telegram_id AND u.is_active=1)
                 OR EXISTS(SELECT 1 FROM event_metadata m WHERE m.item_id=reminder_jobs.item_id AND (m.deadline_at<=? OR m.deadline_at IS NULL OR m.registration_status='closed')))""",(now,))
            row=self._conn.execute("""SELECT j.*,s.payload,u.subscription_version,u.subscription_epoch,m.deadline_at,m.deadline_day,m.deadline_precision
                FROM reminder_jobs j JOIN item_refs r ON r.id=j.item_id JOIN seen_items s ON s.item_key=r.item_key
                JOIN users u ON u.telegram_id=j.telegram_id JOIN event_metadata m ON m.item_id=j.item_id
                WHERE j.status='pending' AND j.due_at<=? AND j.available_at<=? ORDER BY j.due_at,j.id LIMIT 1""",(now,now)).fetchone()
            if row:self._conn.execute("UPDATE reminder_jobs SET status='sending' WHERE id=?",(row['id'],))
            return row

    def finish_reminder(self, job, status, available_at=0, message_id=None):
        with self._lock, self._conn:
            if status=='retry':
                user=self._conn.execute('SELECT is_active,subscription_epoch FROM users WHERE telegram_id=?',(job['telegram_id'],)).fetchone()
                saved=self._conn.execute('SELECT 1 FROM saved_items WHERE telegram_id=? AND item_id=?',(job['telegram_id'],job['item_id'])).fetchone()
                cancel=self._conn.execute('SELECT cancel_requested FROM reminder_jobs WHERE id=?',(job['id'],)).fetchone()
                status='pending' if user and user[0] and user[1]==job['subscription_epoch'] and saved and cancel and not cancel[0] else 'cancelled'
            self._conn.execute('UPDATE reminder_jobs SET status=?,available_at=?,message_id=? WHERE id=? AND status=\'sending\'',(status,available_at,message_id,job['id']))
