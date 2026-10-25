"""Online user-database backup and guarded offline restore. Never handles bot queues."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import time
from .storage import protect_catalog, storage_lock


def readonly(path):
    return sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=2)


def verify(path):
    with closing(readonly(path)) as conn:
        if conn.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise ValueError('Invalid user snapshot')
        objects = conn.execute("SELECT type,name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
        if any(kind not in ('table', 'index') or (kind == 'table' and name != 'favorites') for kind, name in objects):
            raise ValueError('Not a Mini App user snapshot')
        if conn.execute('PRAGMA user_version').fetchone()[0] != 1:
            raise ValueError('Unsupported user snapshot version')
        info = conn.execute('PRAGMA table_info(favorites)').fetchall()
        if [r[1] for r in info] != ['telegram_id', 'item_key', 'created_at'] or [r[5] for r in info] != [1, 2, 0] or not all(r[3] for r in info):
            raise ValueError('Invalid favorites schema')
        return {'schema_version': 1, 'favorites': conn.execute('SELECT count(*) FROM favorites').fetchone()[0]}


def copy_database(source, target):
    deadline = time.monotonic() + 30
    def progress(*_):
        if time.monotonic() > deadline:
            raise TimeoutError('User snapshot deadline exceeded')
    with closing(readonly(source)) as src, closing(sqlite3.connect(str(target), timeout=2)) as dst:
        src.backup(dst, pages=128, progress=progress, sleep=0.05)
    verify(target)
    with open(target, 'rb') as file:
        os.fsync(file.fileno())


def backup(source, directory, *, catalog):
    source = protect_catalog(source, catalog)
    verify(source)
    directory = Path(directory)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    final = directory / f'miniapp-{stamp}-{time.time_ns()}.sqlite3'
    partial = Path(str(final) + '.part')
    fd = os.open(partial, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    os.close(fd)
    try:
        copy_database(source, partial)
        os.replace(partial, final)
        fd = os.open(directory, os.O_DIRECTORY)
        try: os.fsync(fd)
        finally: os.close(fd)
    finally:
        partial.unlink(missing_ok=True)
    return final


def restore(source, target, *, catalog, replace=False, safety_dir=None):
    target = protect_catalog(target, catalog)
    source = protect_catalog(source, catalog)
    if source == target or (source.exists() and target.exists() and os.path.samefile(source, target)):
        raise ValueError('Snapshot and target must differ')
    verify(source)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with storage_lock(target, exclusive=True):
        existed = target.exists()
        if existed:
            if not replace or not safety_dir:
                raise ValueError('Existing user storage requires --replace and --safety-dir')
            backup(target, safety_dir, catalog=catalog)
        else:
            fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            os.close(fd)
        try:
            # SQLite backup commits atomically; do not remove live WAL/SHM files or rename an open DB.
            copy_database(source, target)
        except Exception:
            if not existed:
                target.unlink(missing_ok=True)
            raise
    return verify(target)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', default=os.environ.get('MINIAPP_USER_DB_PATH', '/miniapp-data/miniapp.db'))
    parser.add_argument('--catalog', default=os.environ.get('MINIAPP_DB_PATH', '/catalog/bot.db'))
    sub = parser.add_subparsers(dest='command', required=True)
    create = sub.add_parser('backup'); create.add_argument('--dir', default='/miniapp-backups')
    check = sub.add_parser('verify'); check.add_argument('file')
    recover = sub.add_parser('restore'); recover.add_argument('file')
    recover.add_argument('--replace', action='store_true'); recover.add_argument('--safety-dir')
    args = parser.parse_args()
    try:
        if args.command == 'backup':
            result = {'file': backup(args.db, args.dir, catalog=args.catalog).name}
        elif args.command == 'verify':
            result = verify(args.file)
        else:
            result = restore(args.file, args.db, catalog=args.catalog, replace=args.replace, safety_dir=args.safety_dir)
        print(json.dumps(result))
    except (OSError, sqlite3.Error, ValueError, TimeoutError):
        raise SystemExit('Mini App maintenance failed; verify paths, schema, permissions and stop API before restore') from None


if __name__ == '__main__': main()
