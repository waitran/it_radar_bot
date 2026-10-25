"""CI secret/state checks report safe filenames, never credential values."""
import json
from pathlib import Path
import tempfile
import unittest
from scripts.check_repository import violations


class RepositoryChecksTests(unittest.TestCase):
    def test_private_state_and_dotenv_are_rejected_but_template_is_allowed(self):
        with tempfile.TemporaryDirectory() as directory:
            paths=['.env','.env.production','state.db','copy.sqlite3','state.db.lock','copy.sqlite3-wal','STATE.DB','.env-prod','.env.example']
            for name in paths:(Path(directory)/name).write_text('placeholder')
            self.assertEqual({p['path'] for p in violations(directory,paths)},set(paths)-{'.env.example'})

    def test_credential_split_across_chunks_is_detected_without_echo(self):
        token='123456789:'+('synthetic_'*5)
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'source.py';path.write_text(' '*(65536-10)+token)
            result=violations(directory,['source.py'])
            self.assertEqual(result,[{'path':'source.py','reason':'potential credential'}])
            self.assertNotIn(token,json.dumps(result))

    def test_tracked_symlink_is_rejected_without_reading_external_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'source.py';path.symlink_to('/does/not/exist')
            self.assertEqual(violations(directory,['source.py']),[{'path':'source.py','reason':'tracked symlink'}])

    def test_environment_loading_and_ordinary_code_are_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory)/'source.py').write_text('token = os.environ.get("TELEGRAM_BOT_TOKEN")')
            self.assertEqual(violations(directory,['source.py']),[])
