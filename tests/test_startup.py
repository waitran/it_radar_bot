"""Startup/security checks. Synthetic credentials only; no Telegram requests."""
import io
import logging
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import quote

import requests

from app.config import Config, ConfigurationError
from app.logging_config import RedactingFormatter, configure_logging


ROOT = Path(__file__).resolve().parents[1]
TOKEN = "123456789:" + "synthetic_test_value_" * 2
ENV = {"TELEGRAM_BOT_TOKEN": TOKEN, "OWNER_TELEGRAM_ID": "12345"}


class StartupTests(unittest.TestCase):
    def load(self, **values):
        with patch.dict(os.environ, {**ENV, **values}, clear=True):
            return Config.load(str(ROOT / "config.yaml"))

    def test_environment_and_repr(self):
        cfg = self.load(TELEGRAM_BOT_TOKEN="  " + TOKEN + "  ", DB_PATH="custom.db")
        cfg.validate_runtime()
        self.assertEqual(cfg.bot_token, TOKEN)
        self.assertEqual(cfg.owner_id, 12345)
        self.assertEqual(cfg.db_path, "custom.db")
        self.assertNotIn(TOKEN, repr(cfg))

    def test_config_can_be_loaded_without_credentials_for_offline_rules(self):
        with patch.dict(os.environ, {}, clear=True):
            cfg = Config.load(str(ROOT / "config.yaml"))
        with self.assertRaisesRegex(ConfigurationError, "TELEGRAM_BOT_TOKEN"):
            cfg.validate_runtime()

    def test_invalid_runtime_settings_do_not_echo_values(self):
        for name, value in [("TELEGRAM_BOT_TOKEN", "private-invalid-input"),
                            ("OWNER_TELEGRAM_ID", "private-invalid-input"),
                            ("OWNER_TELEGRAM_ID", "-1"),
                            ("DB_PATH", " ")]:
            with self.subTest(name=name, kind="invalid"):
                with self.assertRaises(ConfigurationError) as exc:
                    self.load(**{name: value}).validate_runtime()
                self.assertIn(name, str(exc.exception))
                if "private" in value:
                    self.assertNotIn(value, str(exc.exception))

    def test_owner_is_optional(self):
        cfg = self.load(OWNER_TELEGRAM_ID="0")
        cfg.validate_runtime()
        self.assertEqual(cfg.owner_id, 0)

    def test_invalid_numbers_are_safe(self):
        for name in ["POLL_INTERVAL_SECONDS", "REQUEST_TIMEOUT_SECONDS"]:
            for value in ["private-invalid-input", "-1"]:
                with self.subTest(name=name):
                    with self.assertRaises(ConfigurationError) as exc:
                        self.load(**{name: value})
                    self.assertIn(name, str(exc.exception))
                    self.assertNotIn(value, str(exc.exception))
        for name in ["POLL_INTERVAL_SECONDS", "REQUEST_TIMEOUT_SECONDS"]:
            with self.assertRaises(ConfigurationError):
                self.load(**{name: "0"})

    def test_yaml_does_not_supply_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.yaml"
            config.write_text(f"bot_token: {TOKEN}\nowner_id: 12345\n", encoding="utf-8")
            with patch.dict(os.environ, {}, clear=True):
                cfg = Config.load(str(config))
            self.assertEqual(cfg.bot_token, "")
            self.assertEqual(cfg.owner_id, 0)

    def cli(self, env, *args):
        return subprocess.run([sys.executable, "-m", "app.main", *args], cwd=ROOT,
                              env={"PYTHONDONTWRITEBYTECODE": "1", **env},
                              capture_output=True, text=True, timeout=15)

    def test_diagnostic_without_network_or_database(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "never-created.db"
            # An unreachable proxy makes accidental network use fail this check.
            result = self.cli({**ENV, "DB_PATH": str(db),
                               "HTTPS_PROXY": "http://127.0.0.1:1"}, "--check-config")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(db.exists())
            self.assertNotIn(TOKEN, result.stdout + result.stderr)

    def test_missing_credentials_fail_before_startup(self):
        result = self.cli({})
        self.assertEqual(result.returncode, 2)
        self.assertIn("TELEGRAM_BOT_TOKEN", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_cli_invalid_value_is_not_printed(self):
        result = self.cli({**ENV, "OWNER_TELEGRAM_ID": TOKEN}, "--check-config")
        self.assertEqual(result.returncode, 2)
        self.assertIn("OWNER_TELEGRAM_ID", result.stderr)
        self.assertNotIn(TOKEN, result.stderr)

    def test_cli_does_not_dump_malformed_yaml(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "bad.yaml"
            config.write_text("secret: [private-arbitrary-value", encoding="utf-8")
            result = self.cli({**ENV, "CONFIG_PATH": str(config)}, "--check-config")
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("private-arbitrary-value", result.stderr)


class LoggingTests(unittest.TestCase):
    def setUp(self):
        self.stream = io.StringIO()
        self.logger = logging.Logger("audit-security", level=logging.DEBUG)
        handler = logging.StreamHandler(self.stream)
        handler.setFormatter(RedactingFormatter("%(levelname)s %(name)s %(message)s"))
        self.logger.addHandler(handler)

    def test_exception_args_url_and_traceback(self):
        with patch.dict(os.environ, ENV, clear=True):
            try:
                raise requests.ReadTimeout(f"https://api.telegram.org/bot{TOKEN}/sendMessage")
            except requests.ReadTimeout as exc:
                self.logger.warning("request failed: %s", exc)
                self.logger.exception("polling %s", TOKEN)
        text = self.stream.getvalue()
        self.assertNotIn(TOKEN, text)
        self.assertIn("[REDACTED]", text)
        self.assertIn("ReadTimeout", text)
        self.assertIn("sendMessage", text)

    def test_unconfigured_and_url_encoded_tokens(self):
        github_token = "github_pat_" + "synthetic" * 8
        with patch.dict(os.environ, {}, clear=True):
            self.logger.info("%s %s %s", f"/bot{TOKEN}/getUpdates", quote(TOKEN, safe=""), github_token)
        text = self.stream.getvalue()
        for value in [TOKEN, quote(TOKEN, safe=""), github_token]:
            self.assertNotIn(value, text)
        self.assertEqual(text.count("[REDACTED]"), 3)

    def test_malformed_secret_and_other_environment_secrets(self):
        value = "private value / with punctuation"
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": value, "SERVICE_API_KEY": "second-private-value"}, clear=True):
            self.logger.error("%s %s second-private-value", value, quote(value, safe=""))
        text = self.stream.getvalue()
        for secret in [value, quote(value, safe=""), "second-private-value"]:
            self.assertNotIn(secret, text)

    def test_configure_logging_covers_library_loggers(self):
        root = logging.getLogger()
        old_handlers, old_level = root.handlers[:], root.level
        try:
            with patch("sys.stderr", self.stream), patch.dict(os.environ, ENV, clear=True):
                configure_logging()
                logging.getLogger("urllib3.connectionpool").warning("failed URL /bot%s/getUpdates", TOKEN)
            self.assertNotIn(TOKEN, self.stream.getvalue())
            self.assertIn("urllib3.connectionpool", self.stream.getvalue())
        finally:
            for handler in root.handlers:
                handler.close()
            root.handlers = old_handlers
            root.setLevel(old_level)

    def test_fatal_startup_exception_is_redacted(self):
        script = (
            "import os,runpy; from unittest.mock import patch; "
            "from requests import ConnectionError; "
            "error=ConnectionError('https://api.telegram.org/bot'+os.environ['TELEGRAM_BOT_TOKEN']+'/getUpdates'); "
            "p=patch('app.db.Database',side_effect=error); p.start(); "
            "runpy.run_module('app.main',run_name='__main__')"
        )
        result = subprocess.run([sys.executable, "-c", script], cwd=ROOT,
                                env={**ENV, "PYTHONDONTWRITEBYTECODE": "1"},
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 1)
        self.assertNotIn(TOKEN, result.stderr)
        self.assertIn("[REDACTED]", result.stderr)
        self.assertIn("ConnectionError", result.stderr)


if __name__ == "__main__":
    unittest.main()
