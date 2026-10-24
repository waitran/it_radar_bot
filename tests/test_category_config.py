"""B04: reject invalid category labels before startup, with safe diagnostics."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import yaml
from app.config import Config, ConfigurationError

ROOT=Path(__file__).resolve().parents[1]


class CategoryConfigTests(unittest.TestCase):
    def load(self, category):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'config.yaml'
            path.write_text(yaml.safe_dump({'rules':{'categories':{'internships':category}}},allow_unicode=True))
            with patch.dict(os.environ,{},clear=True):return Config.load(str(path))

    def test_bad_types_and_blank_labels_are_rejected_without_echo(self):
        for value in (None,123,False,[],['private-invalid-value'],{'private':'value'},'', ' \t\n'):
            with self.subTest(kind=type(value).__name__),self.assertRaises(ConfigurationError) as error:
                self.load({'label':value,'keywords':['стажировка']})
            self.assertIn('rules.categories.internships.label',str(error.exception))
            self.assertNotIn('private-invalid-value',str(error.exception))

    def test_unicode_and_default_labels_are_preserved(self):
        self.assertEqual(self.load({'label':'Стажировки 👋','keywords':[]}).rules.categories['internships']['label'],'Стажировки 👋')
        self.assertEqual(self.load({'keywords':[]}).rules.categories['internships']['label'],'internships')

    def test_real_check_config_cli_exits_before_creating_database(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'config.yaml';db=Path(directory)/'never-created.db'
            path.write_text('rules:\n  categories:\n    internships:\n      label: [private-invalid-value]\n')
            env={'CONFIG_PATH':str(path),'DB_PATH':str(db),'TELEGRAM_BOT_TOKEN':'123456789:'+'synthetic_value_'*2}
            result=subprocess.run([sys.executable,'-m','app.main','--check-config'],cwd=ROOT,env=env,capture_output=True,text=True,timeout=10)
            self.assertEqual(result.returncode,2)
            self.assertIn('rules.categories.internships.label',result.stderr)
            self.assertNotIn('private-invalid-value',result.stderr)
            self.assertFalse(db.exists())
