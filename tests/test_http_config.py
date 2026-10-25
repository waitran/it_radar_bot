"""A09: effective collector settings, with no external services."""
import os
from pathlib import Path
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import Mock, patch

from app.collectors.base import (collection_scope, http_get, http_post_json,
                                 _http_settings)
from app.config import Config, ConfigurationError, DEFAULT_REQUEST_TIMEOUT, DEFAULT_USER_AGENT
from app.main import Bot

ROOT = Path(__file__).resolve().parents[1]


class HttpConfigTests(unittest.TestCase):
    def load(self, **env):
        with patch.dict(os.environ, env, clear=True):
            return Config.load(str(ROOT/'config.yaml'))

    def response(self):
        return Mock(status_code=200, headers={}, raw=Mock(stream=Mock(return_value=iter(()))))

    def test_environment_is_used_for_get_and_post_through_bot(self):
        cfg = self.load(REQUEST_TIMEOUT_SECONDS='7', USER_AGENT='Radar-Test/9')
        with tempfile.TemporaryDirectory() as directory:
            cfg.db_path = str(Path(directory)/'state.db')
            bot = Bot(cfg)
            self.addCleanup(bot.db.close)
            for method in ('get', 'post'):
                with patch('app.collectors.base._pace'), patch('app.collectors.base._request', return_value=self.response()) as send:
                    def collect():
                        if method == 'get': http_get('https://example.test')
                        else: http_post_json('https://example.test', {})
                        return []
                    self.assertEqual(bot.collect_source('test', 'test', collect), [])
                self.assertEqual(send.call_args.kwargs['timeout'], 7)
                self.assertEqual(send.call_args.kwargs['headers']['User-Agent'], 'Radar-Test/9')

    def test_nested_collector_inherits_settings_and_budget(self):
        with patch('app.collectors.base._pace'), patch('app.collectors.base._request', return_value=self.response()) as send:
            with collection_scope(budget=10, request_timeout=70, user_agent='Outer/1'):
                with collection_scope(): http_get('https://example.test')
            self.assertLessEqual(send.call_args.kwargs['timeout'],5)
            self.assertEqual(send.call_args.kwargs['headers']['User-Agent'],'Outer/1')

    def test_explicit_request_timeout_is_supported(self):
        with patch('app.collectors.base._pace'), patch('app.collectors.base._request', return_value=self.response()) as send:
            with collection_scope(request_timeout=7): http_get('https://example.test',timeout=2)
        self.assertEqual(send.call_args.kwargs['timeout'],2)

    def test_settings_reset_after_exception(self):
        before = _http_settings.get()
        with self.assertRaises(RuntimeError), collection_scope(request_timeout=3,user_agent='Temporary/1'):
            raise RuntimeError('test')
        self.assertEqual(_http_settings.get(),before)
        with patch('app.collectors.base._pace'), patch('app.collectors.base._request', return_value=self.response()) as send:
            http_get('https://example.test')
        self.assertEqual(send.call_args.kwargs['timeout'], DEFAULT_REQUEST_TIMEOUT)
        self.assertEqual(send.call_args.kwargs['headers']['User-Agent'], DEFAULT_USER_AGENT)

    def test_parallel_contexts_do_not_share_settings(self):
        barrier = threading.Barrier(2)
        def read(value):
            with collection_scope(request_timeout=value,user_agent=f'Test/{value}'):
                barrier.wait(timeout=5)
                return _http_settings.get()
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(list(pool.map(read,[3,7])),[(3,'Test/3'),(7,'Test/7')])
        self.assertEqual(_http_settings.get(),(DEFAULT_REQUEST_TIMEOUT,DEFAULT_USER_AGENT))

    def test_default_config_and_http_client_agree(self):
        cfg = self.load()
        self.assertEqual(cfg.request_timeout_seconds, DEFAULT_REQUEST_TIMEOUT)
        self.assertEqual(cfg.user_agent, DEFAULT_USER_AGENT)

    def test_invalid_user_agent_fails_without_echoing_value(self):
        for value in ('', ' ', ' leading-space', 'private\r\nInjected: secret', 'кириллица', 'x'*513, 'private\x7f'):
            with self.subTest(value_length=len(value)), self.assertRaises(ConfigurationError) as error:
                self.load(USER_AGENT=value)
            self.assertIn('USER_AGENT',str(error.exception))
            if value.strip(): self.assertNotIn(value,str(error.exception))

    def test_removed_lookback_is_ignored_with_safe_warning(self):
        for value in ('0','72','private-invalid-value'):
            with self.assertLogs('app.config',level='WARNING') as logs:
                cfg = self.load(FIRST_RUN_LOOKBACK_HOURS=value)
            self.assertFalse(hasattr(cfg,'first_run_lookback_hours'))
            self.assertIn('FIRST_RUN_LOOKBACK_HOURS',logs.output[0])
            self.assertNotIn(value,logs.output[0])

    def test_telegram_cycle_uses_configured_network_settings(self):
        cfg = self.load(REQUEST_TIMEOUT_SECONDS='9',USER_AGENT='Cycle/9')
        cfg.websites = []
        cfg.channels = cfg.channels[:1]
        with tempfile.TemporaryDirectory() as directory:
            cfg.db_path = str(Path(directory)/'state.db')
            bot = Bot(cfg)
            self.addCleanup(bot.db.close)
            def fetch(*args,**kwargs):
                http_get('https://example.test')
                return []
            with patch('app.main.telegram_channel.fetch_channel',side_effect=fetch), patch('app.collectors.base._pace'), patch('app.collectors.base._request',return_value=self.response()) as send:
                bot.run_cycle()
            send.assert_called_once()
            self.assertEqual(send.call_args.kwargs['timeout'],9)
            self.assertEqual(send.call_args.kwargs['headers']['User-Agent'],'Cycle/9')

