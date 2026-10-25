"""Consistent online SQLite snapshots and explicit offline restoration."""
from contextlib import closing
from datetime import datetime, timezone
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import sqlite3
import time

from app.db import SCHEMA_VERSION


class BackupCancelled(RuntimeError):
    pass


def _readonly(path):
    return sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,timeout=5)


def _verify(conn):
    if conn.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
        raise ValueError('SQLite snapshot failed quick_check')
    tables={row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {'seen_items','flags'} <= tables:
        raise ValueError('Not an IT Radar database')
    version=conn.execute('PRAGMA user_version').fetchone()[0]
    if version > SCHEMA_VERSION:
        raise ValueError('Snapshot schema is newer than the application')
    return dict(schema_version=version,items=conn.execute('SELECT COUNT(*) FROM seen_items').fetchone()[0],
                users=conn.execute('SELECT COUNT(*) FROM users').fetchone()[0] if 'users' in tables else 0)


def verify_backup(path):
    with closing(_readonly(path)) as conn:
        return _verify(conn)


def _copy(source, target, stop=None, budget=120):
    deadline=time.monotonic()+budget
    def cancelled():
        return (stop is not None and stop.is_set()) or time.monotonic() >= deadline
    def progress(*args):
        if cancelled():raise BackupCancelled('Snapshot interrupted')
    progress()
    with closing(_readonly(source)) as src, closing(sqlite3.connect(str(target),timeout=5)) as dst:
        dst.set_progress_handler(lambda:int(cancelled()),1000)
        src.backup(dst,pages=256,progress=progress,sleep=0.1)
        _verify(dst)
    with open(target,'rb') as stream:os.fsync(stream.fileno())


def create_backup(source, directory, *, keep=7, stop=None, safety=False):
    if keep < 2:raise ValueError('Keep at least two completed snapshots')
    directory=Path(directory)
    directory.mkdir(mode=0o700,parents=True,exist_ok=True)
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    prefix='it-radar-before-restore' if safety else 'it-radar'
    name=f'{prefix}-{stamp}-{time.time_ns()}.sqlite3'
    complete=directory/name;partial=directory/(name+'.part')
    descriptor=os.open(partial,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600);os.close(descriptor)
    try:
        _copy(source,partial,stop)
        os.replace(partial,complete)
        descriptor=os.open(directory,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(descriptor)
        finally:os.close(descriptor)
    finally:
        partial.unlink(missing_ok=True)
    if not safety:
        managed=sorted((path for path in directory.iterdir()
            if re.fullmatch(r'it-radar-\d{8}T\d{6}Z-\d+\.sqlite3',path.name)
            and path.is_file() and not path.is_symlink() and path != complete),reverse=True)
        for old in managed[keep-1:]:old.unlink()
    return complete


def restore_backup(source, target, *, replace=False, safety_dir=None):
    """Never overwrite a running DB; replacements first preserve the old state."""
    source,target=Path(source),Path(target)
    if source.resolve() == target.resolve():raise ValueError('Source and target must differ')
    verify_backup(source)
    target.parent.mkdir(parents=True,exist_ok=True)
    with open(str(target)+'.lock','a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise RuntimeError('Stop the bot before restoration') from None
        existed=target.exists()
        if existed:
            if not replace or not safety_dir:
                raise ValueError('Existing DB requires --replace and --safety-dir')
            create_backup(target,safety_dir,safety=True)
        else:
            descriptor=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600);os.close(descriptor)
        prepared=target.parent/f'.restore-{time.time_ns()}.sqlite3.part'
        descriptor=os.open(prepared,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600);os.close(descriptor)
        try:
            _copy(source,prepared)
            # A snapshot cannot know what Telegram delivered after its timestamp.
            # Quarantine old tasks and establish a fresh subscription boundary
            # in the prepared DB, before atomically copying it into the target.
            with closing(sqlite3.connect(prepared)) as conn, conn:
                tables={row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if 'user_deliveries' in tables:
                    conn.execute("UPDATE user_deliveries SET status='legacy_review',reason='restored_snapshot_review' WHERE status IN ('pending','sending','suspended')")
                columns={row[1] for row in conn.execute('PRAGMA table_info(seen_items)')}
                if 'delivery_state' in columns:
                    conn.execute("UPDATE seen_items SET delivery_state='legacy_review' WHERE delivery_state IN ('pending','sending')")
                if 'reminder_jobs' in tables:
                    conn.execute("UPDATE reminder_jobs SET status='uncertain' WHERE status IN ('pending','sending')")
                if 'delivery_fanout' in tables:conn.execute('DELETE FROM delivery_fanout')
                now=time.time()
                if 'users' in tables:
                    conn.execute('UPDATE users SET subscribed_at=?,updated_at=? WHERE is_active=1',(now,now))
                else:
                    conn.execute("INSERT INTO flags VALUES('enabled','0') ON CONFLICT(name) DO UPDATE SET value='0'")
                conn.execute("INSERT INTO flags VALUES('restored_at',?) ON CONFLICT(name) DO UPDATE SET value=excluded.value",(str(now),))
            _copy(prepared,target)
        except Exception:
            if not existed:target.unlink(missing_ok=True)
            raise
        finally:
            prepared.unlink(missing_ok=True)
    return verify_backup(target)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    create=sub.add_parser('create');create.add_argument('--db',required=True);create.add_argument('--dir',required=True);create.add_argument('--keep',type=int,default=7)
    verify=sub.add_parser('verify');verify.add_argument('file')
    restore=sub.add_parser('restore');restore.add_argument('file');restore.add_argument('--target',required=True)
    restore.add_argument('--replace',action='store_true');restore.add_argument('--safety-dir')
    args=parser.parse_args()
    if args.command=='create':
        path=create_backup(args.db,args.dir,keep=args.keep)
        print(json.dumps(dict(file=path.name,**verify_backup(path))))
    elif args.command=='verify':print(json.dumps(verify_backup(args.file)))
    else:print(json.dumps(restore_backup(args.file,args.target,replace=args.replace,safety_dir=args.safety_dir)))


if __name__=='__main__':main()
