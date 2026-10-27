"""Offline operational acceptance with temporary synthetic snapshots only."""
import errno
from datetime import datetime, timezone
import json
from pathlib import Path
import time
from unittest.mock import patch

from app.backup import create_backup
from app.ops_verify import drill_pair, fingerprints, BOT_QUERIES, snapshot_age, health_transition
from app.healthcheck import write_health
from miniapp.backend.favorites import Favorites
from miniapp.backend.maintenance import backup
from tests.personal_support import PersonalCase
import tests.test_healthcheck as health_fixture


class OpsVerificationTests(PersonalCase):
    def snapshots(self):
        entry, ref = self.event()
        self.db.save_item(101, ref)
        prefs = self.db.preferences(101)
        prefs['timezone'] = 'Asia/Tokyo'
        self.db.set_preferences(101, prefs)
        self.db.user_control(101, reply='synthetic receipt', offset=123)
        self.db.set_reply_state(101, 'uncertain')
        self.db.enable_reminder(101, ref, 1)
        self.db._conn.execute("UPDATE reminder_jobs SET status='sent',message_id=23")
        self.db._conn.commit()
        self.bot.fill_delivery_queue(datetime.now(timezone.utc))
        for row in list(self.db._conn.execute('SELECT id FROM user_deliveries')):
            self.db.finish_user_delivery(row[0], 'sent', 'synthetic', message_id=24)
        user_path = Path(self.tmp.name)/'miniapp.db'
        favorites = Favorites(user_path, self.cfg.db_path)
        favorites.save(101, entry.item_key)
        bot_dir, mini_dir = Path(self.tmp.name)/'bot-backups', Path(self.tmp.name)/'mini-backups'
        return (create_backup(self.cfg.db_path, bot_dir),
                backup(user_path, mini_dir, catalog=self.cfg.db_path))

    def test_both_restores_preserve_content_not_just_counts(self):
        bot, mini = self.snapshots()
        result = drill_pair(bot, mini)
        self.assertTrue(result['ok'])
        for name in ('users', 'preferences', 'saved', 'favorites', 'terminal_deliveries',
                     'terminal_reminders', 'terminal_commands', 'offset_cursors'):
            self.assertGreater(result['preserved_counts'][name], 0, name)
        # Same counts, changed contents must fail.
        from app.backup import restore_backup
        def corrupt(source, target):
            result = restore_backup(source, target)
            import sqlite3
            with sqlite3.connect(target) as conn:
                conn.execute("UPDATE user_preferences SET payload='{}'")
            return result
        with patch('app.ops_verify.restore_backup', side_effect=corrupt):
            with self.assertRaisesRegex(ValueError, 'content mismatch'):
                drill_pair(bot, mini)

    def test_backup_age_missing_stale_corrupt_and_future(self):
        bot, mini = self.snapshots()
        now = time.time()
        for path, kind in ((bot, 'it-radar'), (mini, 'miniapp')):
            self.assertTrue(snapshot_age(path.parent, kind, 3600, now=now)['ok'])
            with self.assertRaises(ValueError):
                snapshot_age(path.parent, kind, 10, now=now+3601)
            with self.assertRaises(ValueError):
                snapshot_age(path.parent, kind, 3600, now=now-120)
        bot.write_bytes(b'corrupt synthetic snapshot')
        with self.assertRaises(Exception):
            snapshot_age(bot.parent, 'it-radar', 3600)
        empty = Path(self.tmp.name)/'empty'; empty.mkdir()
        with self.assertRaises(ValueError):
            snapshot_age(empty, 'it-radar', 3600)

    def test_failed_backup_enospc_leaves_no_completed_snapshot(self):
        directory = Path(self.tmp.name)/'failed-backups'
        with patch('app.backup._copy', side_effect=OSError(errno.ENOSPC, 'synthetic full disk')):
            with self.assertRaises(OSError):
                create_backup(self.cfg.db_path, directory)
        self.assertEqual(list(directory.iterdir()), [])
        with self.assertRaises(ValueError):
            snapshot_age(directory, 'it-radar', 3600)

    def test_health_failure_and_recovery_transitions(self):
        state = health_fixture.HealthcheckTests().state()
        health = Path(self.tmp.name)/'health.json'
        last = Path(self.tmp.name)/'last.json'
        write_health(health, state)
        self.assertIsNone(health_transition(health, last, now=1001)['event'])
        for changes, reason in (({'disk_free_bytes': 0, 'min_free_disk_bytes': 1}, 'disk_space_low'),
                                ({'backup_enabled': True, 'backup_error': 'OSError'}, 'backup_failed'),
                                ({'dispatcher_at': 800}, 'dispatcher_stalled'),
                                ({'backup_enabled': True, 'last_backup_at': 1, 'backup_interval': 10}, 'backup_stale')):
            failing = {**state, **changes}
            write_health(health, failing)
            result = health_transition(health, last, now=1001)
            self.assertEqual((result['event'], result['reason']), ('unhealthy', reason))
            self.assertIsNone(health_transition(health, last, now=1001)['event'])
            write_health(health, state)
            self.assertEqual(health_transition(health, last, now=1001)['event'], 'recovery')
        health.unlink()
        self.assertEqual(health_transition(health, last, now=1001)['event'], 'unhealthy')
