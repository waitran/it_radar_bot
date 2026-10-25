"""Progress monitoring without opening SQLite or starting a second poller."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from app.healthcheck import evaluate, write_health, health_path

ROOT=Path(__file__).resolve().parents[1]


class HealthcheckTests(unittest.TestCase):
    def state(self):
        return dict(version=1,ready=True,updated_at=1000,started_at=900,dispatcher_at=1000,
            poll_expected=True,poll_at=1000,poll_retry_until=0,source_deadlines=[1600],
            scan_report={'attempted':10,'successful':9},scan_error=False,
            queue={'ready':0,'oldest_ready_at':None},max_queue_age=86400,
            backup_enabled=False,backup_error=None,last_backup_at=0,backup_interval=86400)

    def test_healthy_during_bounded_source_work_and_allowed_poll_backoff(self):
        state=self.state();self.assertEqual(evaluate(state,1001),(True,'ok'))
        state.update(poll_at=800,poll_retry_until=1200)
        self.assertEqual(evaluate(state,1001),(True,'ok'))

    def test_stalled_components_and_failed_sources_are_detected(self):
        for changes,reason in [({'updated_at':800},'heartbeat_stale'),({'dispatcher_at':800},'dispatcher_stalled'),
                ({'poll_at':800},'polling_stalled'),({'source_deadlines':[999]},'source_budget_exceeded'),
                ({'scan_error':True},'scan_failed'),({'scan_report':{'attempted':10,'successful':0}},'all_sources_unavailable')]:
            state=self.state();state.update(changes)
            with self.subTest(reason=reason):self.assertEqual(evaluate(state,1001),(False,reason))

    def test_late_queue_and_failed_or_missing_backup_are_detected(self):
        state=self.state();state.update(queue={'ready':1,'oldest_ready_at':1},max_queue_age=100)
        self.assertEqual(evaluate(state,1001),(False,'delivery_queue_late'))
        state=self.state();state.update(backup_enabled=True,backup_error='PermissionError')
        self.assertEqual(evaluate(state,1001),(False,'backup_failed'))
        state.update(backup_error=None,started_at=1)
        self.assertEqual(evaluate(state,1001),(False,'backup_missing'))
        state.update(last_backup_at=1,backup_interval=100)
        self.assertEqual(evaluate(state,1001),(False,'backup_stale'))

    def test_missing_or_corrupt_file_never_creates_or_locks_database(self):
        with tempfile.TemporaryDirectory() as directory:
            db=Path(directory)/'state.db'
            env={'DB_PATH':str(db)}
            for content in (None,'not-json'):
                if content is not None:health_path(db).write_text(content)
                result=subprocess.run([sys.executable,'-m','app.healthcheck'],cwd=ROOT,env=env,capture_output=True,text=True,timeout=5)
                self.assertEqual(result.returncode,1)
                self.assertFalse(db.exists());self.assertFalse(Path(str(db)+'.lock').exists())

    def test_atomic_private_snapshot_and_initializing_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'state.health.json'
            write_health(path,self.state())
            self.assertEqual(json.loads(path.read_text()),self.state())
            self.assertEqual(path.stat().st_mode & 0o777,0o600)
            self.assertEqual(list(Path(directory).glob('.health-*')),[])
            state=self.state();state['ready']=False
            self.assertEqual(evaluate(state,1001),(False,'initializing_or_stopping'))

    def test_malformed_state_is_unhealthy(self):
        for state in ({},[],{'version':1,'ready':True,'updated_at':float('nan')}):
            self.assertFalse(evaluate(state,1001)[0])

    def test_queue_age_uses_reopened_eligibility_instead_of_original_item_age(self):
        from app.db import Database
        from app.models import Item
        with tempfile.TemporaryDirectory() as directory:
            db=Database(str(Path(directory)/'state.db'))
            try:
                db.user_control(101,active=True)
                entry=Item('s','Source','1','https://example.test/jobs/1','Python','','',None)
                db.add_item(entry,category='internships');db.fill_user_queue()
                db._conn.execute("UPDATE seen_items SET first_seen_at='2000-01-01T00:00:00+00:00',eligible_at=1000")
                db._conn.commit()
                metrics=db.health_metrics()
                self.assertEqual(metrics,{'ready':1,'oldest_ready_at':1000})
            finally:db.close()
