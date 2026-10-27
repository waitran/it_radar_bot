"""Offline operational evidence using existing backup/restore implementations.

Never starts a bot/API, sends alerts or overwrites a service database.
"""
import argparse
from contextlib import closing
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import tempfile
import time

from app.backup import verify_backup, restore_backup
from app.healthcheck import evaluate, write_health


def readonly(path):
    return sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro', uri=True)


def snapshot_age(directory, kind, max_age, *, now=None):
    if not math.isfinite(max_age) or max_age <= 0:
        raise ValueError('max_age must be positive and finite')
    now = time.time() if now is None else now
    pattern = re.compile(rf'{kind}-\d{{8}}T\d{{6}}Z-\d+\.sqlite3')
    paths = [p for p in Path(directory).iterdir()
             if pattern.fullmatch(p.name) and not p.is_symlink() and p.is_file()]
    if not paths:
        raise ValueError('Completed backup missing')
    latest = max(paths, key=lambda p: p.stat().st_mtime)
    age = now-latest.stat().st_mtime
    if age < -60 or age > max_age:
        raise ValueError('Backup stale or timestamp in future')
    if kind == 'it-radar':
        verify_backup(latest)
    elif kind == 'miniapp':
        from miniapp.backend.maintenance import verify
        verify(latest)
    else:
        raise ValueError('Unknown backup kind')
    return dict(ok=True, age_seconds=round(max(0, age), 3))


# Only immutable fields: normal restore deliberately changes live queue states
# and active subscription timestamps. Digests stay in memory, never print IDs.
BOT_QUERIES = {
    'users': 'SELECT telegram_id,username,is_active,created_at,subscription_version,subscription_epoch FROM users ORDER BY telegram_id',
    'preferences': 'SELECT * FROM user_preferences ORDER BY telegram_id',
    'saved': 'SELECT * FROM saved_items ORDER BY telegram_id,item_id',
    'aliases': 'SELECT * FROM item_aliases ORDER BY alias,item_key',
    'items': 'SELECT item_key,payload FROM seen_items ORDER BY item_key',
    'terminal_deliveries': "SELECT * FROM user_deliveries WHERE status NOT IN ('pending','sending','suspended') ORDER BY id",
    'terminal_reminders': "SELECT * FROM reminder_jobs WHERE status NOT IN ('pending','sending') ORDER BY id",
    'terminal_commands': "SELECT * FROM command_replies WHERE status NOT IN ('pending','sending') ORDER BY telegram_id",
    'offset_cursors': "SELECT * FROM flags WHERE name='offset' OR name LIKE 'telegram_cursor:%' OR name LIKE 'command_admission_%' ORDER BY name",
}


def fingerprints(path, queries):
    result = {}
    with closing(readonly(path)) as conn:
        for name, query in queries.items():
            digest = hashlib.sha256()
            count = 0
            for row in conn.execute(query):
                digest.update(json.dumps(row, ensure_ascii=True, separators=(',', ':')).encode()+b'\n')
                count += 1
            result[name] = (count, digest.hexdigest())
    return result


def drill_pair(bot_snapshot, miniapp_snapshot):
    from miniapp.backend.maintenance import verify, restore
    started = time.monotonic()
    verify_backup(bot_snapshot)
    verify(miniapp_snapshot)
    before = fingerprints(bot_snapshot, BOT_QUERIES)
    mini_queries = {'favorites': 'SELECT * FROM favorites ORDER BY telegram_id,item_key'}
    mini_before = fingerprints(miniapp_snapshot, mini_queries)
    with tempfile.TemporaryDirectory(prefix='radar-pair-drill-') as directory:
        bot_target = Path(directory)/'bot.sqlite3'
        mini_target = Path(directory)/'miniapp.sqlite3'
        restore_backup(bot_snapshot, bot_target)
        restore(miniapp_snapshot, mini_target, catalog=bot_target)
        if fingerprints(bot_target, BOT_QUERIES) != before:
            raise ValueError('Bot restore content mismatch')
        if fingerprints(mini_target, mini_queries) != mini_before:
            raise ValueError('Mini App restore content mismatch')
    return dict(ok=True, seconds=round(time.monotonic()-started, 3),
                preserved_counts={k: v[0] for k, v in {**before, **mini_before}.items()})


def health_transition(health_file, transition_file, *, now=None):
    try:
        state = json.loads(Path(health_file).read_text())
        healthy, reason = evaluate(state, now)
    except (OSError, ValueError):
        healthy, reason = False, 'health_state_unavailable'
    try:
        previous = json.loads(Path(transition_file).read_text())
    except (OSError, ValueError):
        previous = {}
    if not isinstance(previous, dict):
        previous = {}
    changed = previous.get('healthy') != healthy or previous.get('reason') != reason
    event = ('recovery' if healthy else 'unhealthy') if changed and (previous or not healthy) else None
    write_health(transition_file, dict(healthy=healthy, reason=reason))
    return dict(healthy=healthy, reason=reason, event=event)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    age = sub.add_parser('age')
    age.add_argument('directory')
    age.add_argument('--kind', choices=['it-radar', 'miniapp'], required=True)
    age.add_argument('--max-age', type=float, default=93600)
    pair = sub.add_parser('drill-pair')
    pair.add_argument('bot_snapshot')
    pair.add_argument('miniapp_snapshot')
    health = sub.add_parser('health-transition')
    health.add_argument('health_file')
    health.add_argument('transition_file')
    args = parser.parse_args()
    try:
        if args.command == 'age':
            result = snapshot_age(args.directory, args.kind, args.max_age)
        elif args.command == 'drill-pair':
            result = drill_pair(args.bot_snapshot, args.miniapp_snapshot)
        else:
            result = health_transition(args.health_file, args.transition_file)
        print(json.dumps(result))
        if result.get('healthy') is False:
            raise SystemExit(1)
    except (OSError, ValueError, sqlite3.Error):
        # Safe machine-readable failure: no paths, DB contents or exceptions.
        print(json.dumps(dict(ok=False, reason='operational_verification_failed')))
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
