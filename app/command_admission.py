"""Bounded reply admission; caller owns SQLite lock and transaction.

Business actions and polling offset commit even if UI reply admission fails.
Existing pending work is never evicted. Explicit actions from the same chat
replace its generation; terminal quarantine remains until a new action.
"""
import time

CAPACITY = 1000


def _increment(conn, name):
    conn.execute("""INSERT INTO flags(name,value) VALUES(?, '1')
        ON CONFLICT(name) DO UPDATE SET value=CAST(
        MIN(CAST(flags.value AS INTEGER)+1, 9223372036854775806) AS TEXT)""",
        ('command_admission_' + name,))


def admit(conn, telegram_id):
    row = conn.execute('SELECT status FROM command_replies WHERE telegram_id=?',
                       (telegram_id,)).fetchone()
    if row is not None:
        _increment(conn, 'coalesced')
        return True
    # All statuses count, including uncertain/failed: quarantine cannot grow
    # indefinitely while the network is down. Never delete receipts to make room.
    if conn.execute('SELECT COUNT(*) FROM command_replies').fetchone()[0] >= CAPACITY:
        _increment(conn, 'rejected')
        return False
    _increment(conn, 'accepted')
    return True


def metrics(conn):
    rows, pending, oldest = conn.execute("""SELECT COUNT(*),
        COALESCE(SUM(status='pending'),0),
        MIN(CASE WHEN status IN ('pending','sending') THEN updated_at END)
        FROM command_replies""").fetchone()
    counters = dict(conn.execute("""SELECT name,value FROM flags WHERE name IN (
        'command_admission_accepted','command_admission_coalesced','command_admission_rejected')"""))
    return dict(capacity=CAPACITY, rows=rows, pending=pending,
                oldest_age_seconds=max(0, time.time()-oldest) if oldest is not None else 0,
                **{name: int(counters.get('command_admission_' + name, 0))
                   for name in ('accepted', 'coalesced', 'rejected')})
