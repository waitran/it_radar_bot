"""Synthetic in-memory A07 benchmark; never opens the service DB or calls Telegram.

python -m scripts.benchmark_fanout --users 200 --events 1000
The previous materialization SELECT is compared with a complete current idle fill.
"""
import argparse
import json
from statistics import median
from time import perf_counter

from app.db import Database

OLD_QUERY = """SELECT u.telegram_id,s.item_key FROM seen_items s JOIN users u
    ON u.is_active=1 AND u.subscribed_at<=s.eligible_at
    WHERE s.source_key=? AND s.eligible_at IS NOT NULL AND s.delivery_state='pending'
    AND (s.opportunity_at IS NULL OR julianday(s.opportunity_at)>julianday(u.subscribed_at,'unixepoch'))
    AND NOT EXISTS(SELECT 1 FROM user_deliveries d
        WHERE d.telegram_id=u.telegram_id AND d.item_key=s.item_key)
    ORDER BY s.eligible_at,s.item_key,u.telegram_id LIMIT 500"""


def benchmark(users=100, events=500, repeats=5):
    if users < 1 or events < 1 or repeats < 1 or users*events > 1_000_000:
        raise ValueError('Use positive sizes and at most 1,000,000 synthetic deliveries')
    db=Database(':memory:')
    try:
        db._conn.executemany('INSERT INTO users(telegram_id,is_active,created_at,updated_at,subscribed_at) VALUES(?,1,0,0,1)',((uid,) for uid in range(1,users+1)))
        db._conn.executemany("INSERT INTO seen_items(item_key,source_key,external_id,first_seen_at,delivery_state,eligible_at) VALUES(?,'benchmark',?,'2026-10-24','pending',100)",((str(n),str(n)) for n in range(events)))
        # Represents completed/retired fan-out: no unfinished work remains.
        db._conn.execute("INSERT INTO user_deliveries(telegram_id,item_key,status) SELECT telegram_id,item_key,'sent' FROM users CROSS JOIN seen_items")
        db._conn.commit()
        db.fill_user_queue(available_sources={'benchmark'})
        old_times,new_times=[],[]
        for _ in range(repeats):
            started=perf_counter();db._conn.execute(OLD_QUERY,('benchmark',)).fetchall()
            old_times.append((perf_counter()-started)*1000)
            started=perf_counter();stats=db.fill_user_queue(available_sources={'benchmark'})
            new_times.append((perf_counter()-started)*1000)
        return dict(users=users,events=events,completed_deliveries=users*events,
                    old_select_median_ms=round(median(old_times),3),
                    new_idle_fill_median_ms=round(median(new_times),3),new_fill_stats=stats)
    finally:
        db.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--users',type=int,default=100)
    parser.add_argument('--events',type=int,default=500)
    parser.add_argument('--repeats',type=int,default=5)
    args=parser.parse_args()
    try: result=benchmark(args.users,args.events,args.repeats)
    except ValueError as exc: parser.error(str(exc))
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':main()
