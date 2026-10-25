"""Read-only liveness/progress check. Never opens SQLite or starts Telegram polling."""
import json
import math
import os
from pathlib import Path
import sys
import shutil
import tempfile
import time


def health_path(db_path):
    return Path(db_path).with_suffix('.health.json')


def write_health(path, state):
    path=Path(path)
    descriptor,temporary=tempfile.mkstemp(prefix='.health-',dir=path.parent)
    try:
        with os.fdopen(descriptor,'w') as stream:json.dump(state,stream)
        os.replace(temporary,path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def free_disk(paths):
    values=[]
    for value in paths:
        path=Path(value)
        while not path.exists():path=path.parent
        values.append(shutil.disk_usage(path).free)
    return min(values)


def evaluate(state, now=None):
    now=time.time() if now is None else now
    def number(value):
        if type(value) not in (int,float) or not math.isfinite(value):raise ValueError()
        return value
    try:
        if state['version'] != 1:return False,'invalid_version'
        if not state['ready']:return False,'initializing_or_stopping'
        updated=number(state['updated_at'])
        if now-updated > 90 or updated-now > 60:return False,'heartbeat_stale'
        if now-number(state['dispatcher_at']) > 90:return False,'dispatcher_stalled'
        if state['poll_expected'] and now > max(number(state['poll_at'])+90,number(state['poll_retry_until'])):
            return False,'polling_stalled'
        if any(now > number(deadline) for deadline in state['source_deadlines']):
            return False,'source_budget_exceeded'
        if state['scan_error']:return False,'scan_failed'
        report=state.get('scan_report')
        if report and report['attempted'] > 0 and report['successful'] == 0:
            return False,'all_sources_unavailable'
        if state['queue']['ready'] and now-number(state['queue']['oldest_ready_at']) > number(state['max_queue_age']):
            return False,'delivery_queue_late'
        if state.get('disk_free_bytes') is not None and number(state['disk_free_bytes']) < number(state.get('min_free_disk_bytes',0)):
            return False,'disk_space_low'
        if state['backup_enabled']:
            if state['backup_error']:return False,'backup_failed'
            last=number(state['last_backup_at'])
            if last == 0 and now-number(state['started_at']) > 120:return False,'backup_missing'
            if last and now-last > 2*number(state['backup_interval'])+120:return False,'backup_stale'
        return True,'ok'
    except (KeyError,TypeError,ValueError,OverflowError):
        return False,'invalid_health_state'


def main():
    path=Path(sys.argv[1]) if len(sys.argv)>1 else health_path(os.environ.get('DB_PATH','/data/bot.db'))
    try:
        with path.open() as stream:state=json.load(stream)
        healthy,reason=evaluate(state)
    except (OSError,ValueError):
        healthy,reason=False,'health_state_unavailable'
    print(reason)
    raise SystemExit(0 if healthy else 1)


if __name__=='__main__':main()
