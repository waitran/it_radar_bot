"""Public Telegram fixtures, bounded pagination and restart integration. No network."""
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock

import requests

from app.collectors.telegram_channel import fetch_channel, _parse_page, TelegramSourceError
from app.config import Config, TelegramChannelSource
from app.db import Database
from app.main import Bot
from app.models import Item

FIXTURES = Path(__file__).parent / 'fixtures' / 'telegram'
ROOT = Path(__file__).resolve().parents[1]


def page(ids, before=None, username='example', text='Открыт набор на стажировку Python'):
    posts = ''.join(
        f'<div class="tgme_widget_message_wrap"><div class="tgme_widget_message" data-post="{username}/{i}">'
        f'<time>0:58</time><div class="tgme_widget_message_text">{text}</div>'
        f'<a class="tgme_widget_message_date"><time datetime="2026-10-22T00:00:00+00:00">03:00</time></a>'
        '</div></div>' for i in ids)
    more = (f'<a class="tme_messages_more" data-before="{before}" href="/s/{username}?before={before}"></a>'
            if before is not None else '')
    return f'<section class="tgme_channel_history">{posts}{more}</section>'


def response(html, status=200):
    r = requests.Response()
    r.status_code = status
    r.encoding = 'utf-8'
    r._content = html.encode()
    return r


def item(i):
    return Item('tg_example', 'Example', str(i), f'https://t.me/example/{i}',
                f'Открыт набор на стажировку Python номер {i}', '', None,
                datetime.now(timezone.utc))


class TelegramParsingTests(unittest.TestCase):
    def test_all_seven_real_channel_fixtures(self):
        expected = {'rostelecom_tech': 16, 'rostelecom_molodezh': 9, 'CTDLru': 9,
                    'mgtsstudents': 7, 'brainzbycroc': 14, 'k2cybersec_team': 10, 'rvision_team': 11}
        total = 0
        for username, count in expected.items():
            with self.subTest(channel=username):
                items, _ = _parse_page((FIXTURES / (username + '.html')).read_text(), username)
                self.assertEqual(len(items), count)
                self.assertTrue(all(i.published_at and i.published_at.tzinfo for i in items))
                ids = [int(i.external_id) for i in items]
                self.assertEqual(ids, sorted(set(ids)))
                self.assertTrue(all(i.url == f'https://t.me/{username}/{i.external_id}' for i in items))
                total += len(items)
        self.assertEqual(total, 76)

    def test_video_duration_does_not_hide_date(self):
        items, _ = _parse_page((FIXTURES / 'rostelecom_tech.html').read_text(), 'rostelecom_tech')
        by_id = {i.external_id: i for i in items}
        self.assertEqual(by_id['1895'].published_at.isoformat(), '2026-09-25T09:24:04+00:00')
        self.assertEqual(by_id['1899'].published_at.isoformat(), '2026-10-07T07:54:21+00:00')

    def test_real_registration_and_media_only_post(self):
        items, _ = _parse_page((FIXTURES / 'mgtsstudents.html').read_text(), 'mgtsstudents')
        by_id = {i.external_id: i for i in items}
        self.assertEqual(by_id['16'].apply_url, 'https://studchamp.mgts.ru/')
        self.assertIn('Регистрируйтесь до 15 ноября:', by_id['16'].raw_text)
        self.assertEqual(by_id['17'].raw_text, '')
        self.assertEqual(by_id['17'].title, '')
        self.assertIsNone(by_id['17'].apply_url)

    def test_inline_formatting_linebreaks_and_single_entity_decoding(self):
        items, _ = _parse_page(page([1], text='<b>Python</b> для студентов<br>Заявка &amp;lt;пример&amp;gt;'), 'example')
        self.assertEqual(items[0].title, 'Python для студентов')
        self.assertEqual(items[0].raw_text, 'Python для студентов\nЗаявка &lt;пример&gt;')

    def test_emoji_line_is_not_the_title(self):
        items, _ = _parse_page(page([1], text='🚀<br><b>Набор Python</b> открыт'), 'example')
        self.assertEqual(items[0].title, 'Набор Python открыт')

    def test_registration_context_and_social_footer(self):
        text = ('Регистрируйтесь до 30 октября: <a href="https://forms.example.org/event">ссылка</a>'
                '<br>Наш канал: <a href="https://t.me/example">подписаться</a>')
        items, _ = _parse_page(page([1], text=text), 'example')
        self.assertEqual(items[0].apply_url, 'https://forms.example.org/event')

    def test_no_registration_from_social_or_shortener_without_context(self):
        for url in ['https://t.me/example/100', 'https://max.ru/join/test', 'https://clck.ru/abc']:
            with self.subTest(url=url):
                items, _ = _parse_page(page([1], text=f'Новости <a href="{url}">читать</a>'), 'example')
                self.assertIsNone(items[0].apply_url)

    def test_registration_on_next_line_and_telegram_bot_start(self):
        text = 'Подать заявку:<br><a href="https://t.me/example_bot?start=application">Форма</a>'
        items, _ = _parse_page(page([1], text=text), 'example')
        self.assertEqual(items[0].apply_url, 'https://t.me/example_bot?start=application')

    def test_ambiguous_forms_are_not_guessed(self):
        text = ('<a href="https://example.org/a">Регистрация A</a><br>'
                '<a href="https://example.org/b">Регистрация B</a>')
        items, _ = _parse_page(page([1], text=text), 'example')
        self.assertIsNone(items[0].apply_url)

    def test_unsafe_application_urls_are_rejected(self):
        for url in ['javascript:alert(1)', 'https://name:password@example.org/form',
                    'https://example.org/a&quot;broken']:
            with self.subTest(kind='unsafe URL'):
                items, _ = _parse_page(page([1], text=f'<a href="{url}">Регистрация</a>'), 'example')
                self.assertIsNone(items[0].apply_url)

    def test_changed_html_or_dates_fail_explicitly(self):
        fixtures = [
            '<html>Open in Telegram</html>',
            page([1], username='other'),
            page([1]).replace('data-post=', 'unknown-attr='),
            page([1]).replace(' datetime=', ' unknown-attr='),
            page([1]).replace('2026-10-22T00:00:00+00:00', 'not-a-date'),
            page([1]).replace('2026-10-22T00:00:00+00:00', '2026-10-22'),
        ]
        for html in fixtures:
            with self.subTest(case=fixtures.index(html)):
                with self.assertRaises(TelegramSourceError):
                    _parse_page(html, 'example')


class TelegramPaginationTests(unittest.TestCase):
    def test_initial_snapshot_reads_one_page_oldest_first(self):
        with patch('app.collectors.telegram_channel.http_get', return_value=response(page([12, 10, 11], before=10))) as get:
            result = fetch_channel('example')
        self.assertEqual([i.external_id for i in result], ['10', '11', '12'])
        get.assert_called_once_with('https://t.me/s/example', allow_redirects=False, max_bytes=8_000_000)

    def test_multiple_pages_with_overlap_and_watermark(self):
        with patch('app.collectors.telegram_channel.http_get', side_effect=[
            response(page([9, 10], before=9)), response(page([6, 8, 9], before=6)),
            response(page([3, 4, 5], before=3)),
        ]) as get:
            result = fetch_channel('example', since_id=4)
        self.assertEqual([i.external_id for i in result], ['5', '6', '8', '9', '10'])
        self.assertEqual(get.call_count, 3)
        self.assertEqual(get.call_args_list[1].args[0], 'https://t.me/s/example?before=9')
        self.assertEqual(get.call_args_list[2].args[0], 'https://t.me/s/example?before=6')

    def test_deleted_watermark_and_end_of_history(self):
        for html in [page([7, 9], before=7), page([7, 9])]:
            with patch('app.collectors.telegram_channel.http_get', return_value=response(html)):
                result = fetch_channel('example', since_id=8)
            self.assertEqual([i.external_id for i in result], ['9'])

    def test_no_new_posts_or_deleted_latest_post(self):
        with patch('app.collectors.telegram_channel.http_get', return_value=response(page([7, 8], before=7))) as get:
            self.assertEqual(fetch_channel('example', since_id=9), [])
        self.assertEqual(get.call_count, 1)

    def test_limit_and_repeating_page_never_return_partial_results(self):
        with patch('app.collectors.telegram_channel.http_get', return_value=response(page([9, 10], before=9))):
            with self.assertRaisesRegex(TelegramSourceError, 'max_pages'):
                fetch_channel('example', since_id=1, max_pages=1)
            with self.assertRaisesRegex(TelegramSourceError, 'повторяет'):
                fetch_channel('example', since_id=1)

    def test_later_page_network_failure(self):
        with patch('app.collectors.telegram_channel.http_get', side_effect=[response(page([9], before=9)), requests.Timeout()]):
            with self.assertRaisesRegex(TelegramSourceError, 'Ошибка сети'):
                fetch_channel('example', since_id=1)

    def test_http_denial_and_redirect_do_not_trigger_workarounds(self):
        for status in [301, 302, 401, 403, 429, 500]:
            with self.subTest(status=status):
                with patch('app.collectors.telegram_channel.http_get', return_value=response('', status)) as get:
                    with self.assertRaises(TelegramSourceError):
                        fetch_channel('example')
                self.assertEqual(get.call_count, 1)
        failure = requests.HTTPError(response=response('', 403))
        with patch('app.collectors.telegram_channel.http_get', side_effect=failure):
            with self.assertRaisesRegex(TelegramSourceError, '403'):
                fetch_channel('example')

    def test_unsafe_pagination_is_never_followed(self):
        for link in ['https://other.example/s/example?before=9', '/s/other?before=9',
                     'http://t.me/s/example?before=9', '/s/example?before=9&extra=1',
                     '/s/example?before=10', '/s/example?before=9#fragment']:
            html = page([9], before=9).replace('/s/example?before=9', link)
            with self.subTest(link=link), patch('app.collectors.telegram_channel.http_get', return_value=response(html)) as get:
                with self.assertRaises(TelegramSourceError):
                    fetch_channel('example', since_id=1)
                self.assertEqual(get.call_count, 1)

    def test_invalid_arguments_do_not_make_requests(self):
        with patch('app.collectors.telegram_channel.http_get') as get:
            for name in ['../private', 'example?query=1', 'https://t.me/example']:
                with self.assertRaises(TelegramSourceError):
                    fetch_channel(name)
            for kw in [{'since_id': 0}, {'since_id': '1'}, {'max_pages': 0}, {'max_pages': True}]:
                with self.assertRaises(TelegramSourceError):
                    fetch_channel('example', **kw)
        get.assert_not_called()


class TelegramStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = Config.load(str(ROOT / 'config.yaml'))
        self.cfg.db_path = str(Path(self.tmp.name) / 'state.db')
        self.cfg.channels = [TelegramChannelSource('configured_key', 'example')]
        self.cfg.websites = []
        self.bot = Bot(self.cfg)
        self.addCleanup(lambda: self.bot.db.close())
        self.bot.notifier.send = Mock(return_value=True)

    def test_first_snapshot_is_baseline_even_after_start(self):
        self.bot.set_enabled(True)
        with patch('app.main.telegram_channel.fetch_channel', return_value=[item(1), item(2)]) as fetch:
            self.bot.run_cycle()
        fetch.assert_called_once_with('example', since_id=None, max_pages=25, include_recent=True)
        self.assertEqual(self.bot.db.telegram_cursor('tg_example'), 2)
        self.assertEqual(self.bot.db.count_items(), 2)
        self.bot.notifier.send.assert_not_called()

    def test_restart_continues_from_saved_id_and_keeps_existing_rows(self):
        with patch('app.main.telegram_channel.fetch_channel', return_value=[item(1)]):
            self.bot.run_cycle()
        self.bot.db.close()
        self.bot = Bot(self.cfg)
        self.bot.notifier.send = Mock(return_value=True)
        with patch('app.main.telegram_channel.fetch_channel', return_value=[item(2)]) as fetch:
            self.bot.run_cycle()
        self.assertEqual(fetch.call_args.kwargs['since_id'], 1)
        self.assertEqual(self.bot.db.count_items(), 2)
        self.assertEqual(self.bot.db.telegram_cursor('tg_example'), 2)

    def test_legacy_database_uses_seen_ids_without_deleting_data(self):
        self.bot.db.add_item(item(7), baseline=True)
        with patch('app.main.telegram_channel.fetch_channel', return_value=[]) as fetch:
            self.bot.run_cycle()
        self.assertEqual(fetch.call_args.kwargs['since_id'], 7)
        self.assertEqual(self.bot.db.count_items(), 1)
        self.assertTrue(self.bot.db.exists('tg_example:7'))

    def test_initial_failure_does_not_initialize_or_advance(self):
        with patch('app.main.telegram_channel.fetch_channel', side_effect=TelegramSourceError('max_pages')):
            self.bot.run_cycle()
        self.assertFalse(self.bot.db.source_initialized('configured_key'))
        self.assertIsNone(self.bot.db.telegram_cursor('tg_example'))
        self.assertEqual(self.bot.db.count_items(), 0)
        self.assertIn('configured_key', self.bot.db.last_run()['errors'])

    def test_error_keeps_cursor_and_success_clears_error(self):
        self.bot.db.save_telegram_cursor('tg_example', 7)
        with patch('app.main.telegram_channel.fetch_channel', side_effect=TelegramSourceError('offline')):
            self.bot.run_cycle()
        self.assertEqual(self.bot.db.telegram_cursor('tg_example'), 7)
        self.assertEqual(self.bot.db.get_source_error('configured_key'), 'offline')
        with patch('app.main.telegram_channel.fetch_channel', return_value=[]):
            self.bot.run_cycle()
        self.assertFalse(self.bot.db.get_source_error('configured_key'))
        self.assertEqual(self.bot.db.last_run()['errors'], '')

    def test_interrupted_initial_ingestion_remains_baseline(self):
        self.bot.set_enabled(True)
        process = self.bot.process_item
        def partial(item, **kwargs):
            if item.external_id == '2':
                raise RuntimeError('simulated interruption')
            return process(item, **kwargs)
        with patch('app.main.telegram_channel.fetch_channel', return_value=[item(1), item(2)]), patch.object(self.bot, 'process_item', side_effect=partial):
            self.bot.run_cycle()
        self.assertIsNone(self.bot.db.telegram_cursor('tg_example'))
        self.assertTrue(self.bot.db.exists('tg_example:1'))
        with patch('app.main.telegram_channel.fetch_channel', return_value=[item(1), item(2)]) as fetch:
            self.bot.run_cycle()
        self.assertIsNone(fetch.call_args.kwargs['since_id'])
        self.bot.notifier.send.assert_not_called()
        self.assertEqual(self.bot.db.telegram_cursor('tg_example'), 2)

    def test_partial_ingestion_does_not_advance_legacy_cursor(self):
        self.bot.db.add_item(item(1), baseline=True)
        process = self.bot.process_item
        def partial(item, **kwargs):
            if item.external_id == '3':
                raise RuntimeError('simulated interruption')
            return process(item, **kwargs)
        with patch('app.main.telegram_channel.fetch_channel', return_value=[item(2), item(3)]), patch.object(self.bot, 'process_item', side_effect=partial):
            self.bot.run_cycle()
        self.assertEqual(self.bot.db.telegram_cursor('tg_example'), 1)
        self.assertTrue(self.bot.db.exists('tg_example:2'))

    def test_one_failed_channel_does_not_block_the_next(self):
        self.cfg.channels.append(TelegramChannelSource('second_key', 'second'))
        second = item(1)
        second.source_key = 'tg_second'
        with patch('app.main.telegram_channel.fetch_channel', side_effect=[TelegramSourceError('offline'), [second]]):
            self.bot.run_cycle()
        self.assertIsNone(self.bot.db.telegram_cursor('tg_example'))
        self.assertEqual(self.bot.db.telegram_cursor('tg_second'), 1)

    def test_media_only_id_is_saved_without_notification(self):
        post = item(10)
        post.title = post.raw_text = post.summary = ''
        self.bot.db.save_telegram_cursor('tg_example', 9)
        self.bot.set_enabled(True)
        with patch('app.main.telegram_channel.fetch_channel', return_value=[post]):
            self.bot.run_cycle()
        self.assertEqual(self.bot.db.telegram_cursor('tg_example'), 10)
        self.assertTrue(self.bot.db.exists('tg_example:10'))
        self.bot.notifier.send.assert_not_called()


if __name__ == '__main__':
    unittest.main()
