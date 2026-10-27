"""Real dispatcher/SQLite, virtual network/clock; never a Telegram load test.

python -m scripts.benchmark_delivery_capacity > capacity.json
Use --sizes 100 for a smoke run. All data is synthetic and temporary.
"""
import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from statistics import median
import tempfile
from time import perf_counter
from unittest.mock import patch

from app.config import Config, Rules, TelegramChannelSource
from app.main import Bot
from app.models import Item
from app.notify import DeliveryResult


class TimedConnection:
    """Execute/commit timing only; cursor fetching remains in dispatcher wall time."""
    def __init__(self, connection):
        self.connection = connection
        self.seconds = 0.0

    def __getattr__(self, name):
        value = getattr(self.connection, name)
        if name not in ('execute', 'executemany', 'executescript', 'commit'):
            return value
        def timed(*args, **kwargs):
            started = perf_counter()
            try:
                return value(*args, **kwargs)
            finally:
                self.seconds += perf_counter()-started
        return timed

    def __enter__(self):
        self.connection.__enter__()
        return self

    def __exit__(self, *args):
        started = perf_counter()
        try:
            return self.connection.__exit__(*args)
        finally:
            self.seconds += perf_counter()-started


def percentile(values, fraction):
    return sorted(values)[min(len(values)-1, int((len(values)-1)*fraction))] if values else 0


def measure(count, mixed=False, fault='normal'):
    with tempfile.TemporaryDirectory(prefix='radar-capacity-') as directory:
        cfg = Config('', 0, str(Path(directory)/'synthetic.db'), 600, 30,
                     'synthetic', [TelegramChannelSource('example', 'example')], [], Rules())
        cfg.rules = Config.load(str(Path(__file__).resolve().parents[1]/'config.yaml')).rules
        cfg.item_verification_ttl_seconds = 10**9
        bot = Bot(cfg)
        bot.db._conn = TimedConnection(bot.db._conn)
        fanout_seconds = [0.0]
        fill = bot.db.fill_user_queue
        def timed_fill(*args, **kwargs):
            started = perf_counter()
            try:
                return fill(*args, **kwargs)
            finally:
                fanout_seconds[0] += perf_counter()-started
        bot.db.fill_user_queue = timed_fill
        clock = [datetime.now(timezone.utc).timestamp()]
        start = clock[0]
        attempts, success, queue_waits = [], [], []
        http_seconds = [0.0]
        sql_statements = [0]
        def trace(_):
            sql_statements[0] += 1
        bot.db._conn.set_trace_callback(trace)
        def send(*_, **kwargs):
            kind = bot.db.get_flag('last_send_kind')
            attempts.append((clock[0], kind))
            duration = 2.0 if fault == 'slow' else .05
            clock[0] += duration
            http_seconds[0] += duration
            # Each 100th attempt fails once; bounded deterministic injection.
            if len(attempts) % 100 == 1 and fault in ('429', 'temporary'):
                return DeliveryResult('retry', code=429 if fault == '429' else 503,
                                      retry_after=30 if fault == '429' else 10)
            success.append((clock[0], kind))
            if kind == 'event':
                queue_waits.append(clock[0]-start)
            return DeliveryResult('sent', message_id=len(attempts))
        bot.notifier.send = send
        try:
            with patch('app.main.time.time', side_effect=lambda: clock[0]):
                for uid in range(1, count+1):
                    bot.db.user_control(uid, active=True)
                bot.db._conn.execute('UPDATE users SET subscribed_at=?', (start-100,))
                bot.db._conn.commit()
                deadline = datetime.fromtimestamp(start, timezone.utc)+timedelta(days=6)
                entry = Item('tg_example', 'Synthetic', '1', 'https://t.me/example/1',
                             'Митап Python', 'Для начинающих', 'https://example.invalid/register',
                             datetime.fromtimestamp(start, timezone.utc),
                             raw_text=f'Митап Python состоится {deadline:%Y-%m-%d}.\nРегистрация открыта до {deadline:%Y-%m-%d}. Для начинающих. Приходите!',
                             categories=['events'])
                bot.db.add_item(entry, category='events')
                ref = bot.db.item_ref(entry.item_key)
                bot._fresh_items[entry.item_key] = entry
                bot._verified_at[entry.item_key] = start
                bot._available_sources = {'tg_example'}
                extra = max(1, count//10) if mixed else 0
                for uid in range(1, extra+1):
                    bot.db.user_control(uid, reply='synthetic')
                    bot.db.save_item(uid, ref)
                    if not bot.db.enable_reminder(uid, ref, 1):
                        raise AssertionError('Synthetic reminder setup failed')
                bot.db._conn.execute('UPDATE reminder_jobs SET due_at=0')
                bot.db._conn.commit()
                fanout_start = perf_counter()
                batches = 0
                while True:
                    stats = bot.db.fill_user_queue(available_sources={'tg_example'})
                    batches += 1
                    if not bot.db._conn.execute('SELECT 1 FROM delivery_fanout').fetchone():
                        break
                    # Initial materialization sample stops after one 500-row batch.
                    if bot.db._conn.execute('SELECT count(*) FROM user_deliveries').fetchone()[0] >= 500:
                        break
                fanout_ms = (perf_counter()-fanout_start)*1000
                first_batch = bot.db._conn.execute('SELECT count(*) FROM user_deliveries').fetchone()[0]
                sql_statements[0] = 0
                sqlite_started = bot.db._conn.seconds
                wall = perf_counter()
                expected = count+2*extra
                for _ in range(expected*200):
                    bot._last_scan = clock[0]
                    bot.dispatch_pending(now=datetime.fromtimestamp(clock[0], timezone.utc))
                    if len(success) == expected:
                        break
                    # Same nearest-slot policy; no real sleep and no network.
                    clock[0] += bot.dispatch_delay() if hasattr(bot, 'dispatch_delay') else 2
                wall_ms = (perf_counter()-wall)*1000
                if len(success) != expected:
                    raise AssertionError(f'Incomplete drain {len(success)}/{expected}')
                gaps = [b[0]-a[0] for a,b in zip(attempts, attempts[1:])]
                if gaps and min(gaps) < 1.149:
                    raise AssertionError('Limiter violated')
                elapsed = clock[0]-start
                return dict(recipients=count, mixed=mixed, fault=fault, attempts=len(attempts),
                            delivered=len(success), fanout_first_batch=first_batch,
                            fanout_batches=batches, fanout_wall_ms=round(fanout_ms,3),
                            dispatch_sql_statements=sql_statements[0],
                            dispatch_sqlite_execute_commit_wall_ms=round((bot.db._conn.seconds-sqlite_started)*1000,3),
                            fanout_total_wall_ms=round(fanout_seconds[0]*1000,3),
                            sqlite_file_bytes=Path(cfg.db_path).stat().st_size,
                            dispatch_python_sqlite_wall_ms=round(wall_ms,3),
                            model_elapsed_seconds=round(elapsed,3),
                            model_http_slot_utilization=round(http_seconds[0]/elapsed,4),
                            model_event_wait_p50_seconds=round(median(queue_waits),3),
                            model_event_wait_p95_seconds=round(percentile(queue_waits,.95),3),
                            model_min_attempt_gap_seconds=round(min(gaps),3) if gaps else 0,
                            maximum_http_inflight=1)
        finally:
            bot.db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sizes', nargs='+', type=int, default=[100,1000,5000])
    args = parser.parse_args()
    if any(n < 1 or n > 5000 for n in args.sizes):
        parser.error('sizes must be 1..5000')
    results = [measure(n, mixed, fault) for n in args.sizes for mixed in (False, True)
               for fault in ('normal','429','temporary','slow')]
    print(json.dumps(dict(synthetic_only=True, network='mock', clock='virtual',
                         sqlite='temporary file (TMPDIR controls filesystem)', results=results), indent=2))


if __name__ == '__main__':
    main()
