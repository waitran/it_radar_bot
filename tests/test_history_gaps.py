"""B05: unproven history completeness never advances the persisted cursor."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.config import Config, TelegramChannelSource
from app.main import Bot
from app.collectors.telegram_channel import fetch_channel, TelegramSourceError
from tests.test_delivery import ROOT
from tests.test_telegram import page, response


class HistoryGapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.cfg = Config.load(str(ROOT/'config.yaml'))
        self.cfg.db_path = str(Path(self.tmp.name)/'state.db');self.cfg.owner_id = 0
        self.cfg.channels = [TelegramChannelSource('configured','example')];self.cfg.websites = []
        self.bot = Bot(self.cfg);self.addCleanup(lambda:self.bot.db.close())
        self.bot.db.save_telegram_cursor('tg_example',1)

    def test_changed_or_absent_pagination_never_commits_partial_snapshot(self):
        for html in (page([100,101]),page([100,101],before=100).replace('tme_messages_more','changed-pagination')):
            with patch('app.collectors.telegram_channel.http_get',return_value=response(html)):
                self.bot.run_cycle()
            self.assertEqual(self.bot.db.telegram_cursor('tg_example'),1)
            self.assertEqual(self.bot.db.count_items(),0)
            self.assertNotIn('tg_example',self.bot._available_sources)
            self.assertIn('Полнота истории',self.bot.db.get_source_error('configured'))

    def test_deleted_boundary_is_safe_if_older_message_is_observed(self):
        with patch('app.collectors.telegram_channel.http_get',return_value=response(page([7,9]))):
            self.assertEqual([entry.external_id for entry in fetch_channel('example',since_id=8)],['9'])

    def test_explicit_gap_survives_restart_without_broadcasting_skipped_history(self):
        self.bot.db.user_control(101,active=True)
        self.bot.db.accept_telegram_gap('tg_example',101)
        record = json.loads(self.bot.db.get_flag('telegram_gap:tg_example'))
        self.assertEqual((record['previous'],record['through']),(1,101))
        self.bot.db.close();self.bot = Bot(self.cfg)
        with patch('app.collectors.telegram_channel.http_get',return_value=response(page([100,101]))):
            self.bot.run_cycle()
        self.assertEqual(self.bot.db.telegram_cursor('tg_example'),101)
        self.assertEqual(self.bot.db.count_items(),2)
        self.assertEqual(self.bot.db.multi_delivery_counts(),{})
        self.assertTrue(all(row[0] in {'baseline','rejected','skipped'} for row in self.bot.db._conn.execute('SELECT delivery_state FROM seen_items')))

    def test_gap_cannot_reset_or_rewind_boundary(self):
        for value in (0,1,-1,None,'101'):
            with self.assertRaises(ValueError):self.bot.db.accept_telegram_gap('tg_example',value)
        self.assertEqual(self.bot.db.telegram_cursor('tg_example'),1)
        with self.assertRaises(ValueError):self.bot.db.accept_telegram_gap('tg_unknown',101)

    def test_offline_cli_requires_configured_source_and_preserves_other_state(self):
        import os, subprocess, sys, yaml
        path = Path(self.tmp.name)/'config.yaml'
        path.write_text(yaml.safe_dump({'telegram_channels':[{'key':'configured','username':'example'}]}))
        self.bot.db.set_flag('offset','77')
        self.bot.db.close()
        env = {'CONFIG_PATH':str(path),'DB_PATH':self.cfg.db_path}
        command = [sys.executable,'-m','app.main','--accept-telegram-gap','example','--through-id','101']
        result = subprocess.run(command,cwd=ROOT,env=env,capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        self.bot = Bot(self.cfg)
        self.assertEqual(self.bot.db.telegram_cursor('tg_example'),101)
        self.assertEqual(self.bot.db.get_flag('offset'),'77')

