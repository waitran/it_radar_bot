"""Help lists commands per sender; no real Telegram requests."""
import unittest
from app.main import HELP
from tests import test_multiuser


class HelpTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_multiuser.MultiuserTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.cfg.owner_id = 101

    def reply(self, uid, command):
        self.fixture.command(uid, command)
        return self.fixture.bot.db.pending_reply(10**12)['text']

    def test_owner_help_includes_admin_command(self):
        text = self.reply(101, '/help')
        self.assertIn(HELP, text)
        self.assertIn('/check — проверить источники', text)

    def test_public_help_hides_admin_command(self):
        text = self.reply(202, '/help')
        self.assertIn(HELP, text)
        self.assertNotIn('/check', text)

    def test_owner_start_status_unknown_also_include_admin_command(self):
        for command in ('/start', '/status', '/unknown'):
            self.assertIn('/check', self.reply(101, command))
