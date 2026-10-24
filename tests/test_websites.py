"""Site fixtures, completeness failures and integration; no real HTTP or secrets."""
from datetime import datetime, timezone
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

import requests
from bs4 import BeautifulSoup

from app.collectors.k2 import K2Collector
from app.collectors.croc import CrocCollector
from app.collectors.rtkit import RtkitCollector
from app.collectors.website_http import WebsiteSourceError, read_text, read_json, job_dates
from app.config import Config, HttpSource
from app.main import Bot
from app.models import Item

FIXTURES = Path(__file__).parent / 'fixtures' / 'websites'
ROOT = Path(__file__).resolve().parents[1]


def fixture(name):
    return (FIXTURES / name).read_text(encoding='utf-8')


def k2_source():
    return HttpSource('k2', 'K2', 'https://careers.cybersecurity.k2.tech/vacancy', 'k2_vacancy')


def croc_source():
    return HttpSource('croc', 'CROC', 'https://careers.croc.ru/vacancies/?_sft_expirience=0-1-%D0%B3%D0%BE%D0%B4', 'croc_vacancies')


def catalog():
    data = json.loads(fixture('k2_catalog.json'))
    product = next(p for p in data['products'] if 'SOC' in p['title'])
    return {'products': [product], 'total': 1, 'nextslice': None}


class K2Tests(unittest.TestCase):
    def fetch(self, data=None, detail=None, source=None):
        with patch('app.collectors.k2.read_text', side_effect=[fixture('k2_listing.html'), detail or fixture('k2_detail.html')]), \
                patch('app.collectors.k2.read_json', return_value=data if data is not None else catalog()):
            return K2Collector(source or k2_source()).fetch()

    def test_real_catalog_and_full_description(self):
        item = self.fetch()[0]
        self.assertEqual(item.title, 'Стажер-аналитик SOC')
        self.assertEqual(item.external_id, 'stazher-analitik-soc')
        self.assertIn('Наши ожидания', item.raw_text)
        self.assertIn('Мы предлагаем', item.raw_text)
        self.assertGreater(len(item.raw_text), 1500)
        self.assertNotIn('Направления Преимущества О нас', item.raw_text)
        self.assertTrue(item.apply_url.endswith('#form'))
        self.assertIsNone(item.published_at)
        self.assertIsNone(item.deadline)
        self.assertEqual(item.registration_status, 'unknown')

    def test_dates_are_only_from_jobposting(self):
        data = catalog()
        data['products'][0]['lastmod'] = '2000-01-01'
        self.assertIsNone(self.fetch(data)[0].published_at)
        detail = fixture('k2_detail.html') + '<script type="application/ld+json">{"@type":"JobPosting","datePosted":"2026-10-01","validThrough":"2026-10-30"}</script>'
        item = self.fetch(detail=detail)[0]
        self.assertEqual(item.published_at.isoformat(), '2026-10-01T00:00:00+03:00')
        self.assertEqual(item.deadline.isoformat(), '2026-10-30T23:59:59.999999+03:00')

    def test_catalogue_pagination_uses_public_page_configuration(self):
        first = catalog(); first.update(total=2, nextslice='2')
        second = copy.deepcopy(first); second['nextslice'] = None
        second['products'][0]['uid'] = 'second'
        second['products'][0]['url'] = 'https://careers.cybersecurity.k2.tech/second'
        with patch('app.collectors.k2.read_text', side_effect=[fixture('k2_listing.html'), fixture('k2_detail.html'), fixture('k2_detail.html')]), \
                patch('app.collectors.k2.read_json', side_effect=[first, second]) as api:
            items = K2Collector(k2_source()).fetch()
        self.assertEqual(len(items), 2)
        self.assertEqual(parse_qs(urlsplit(api.call_args_list[1].args[0]).query)['slice'], ['2'])
        self.assertIn('storepartuid', parse_qs(urlsplit(api.call_args_list[0].args[0]).query))

    def test_empty_catalog_is_valid_only_with_explicit_zero_total(self):
        self.assertEqual(self.fetch({'products': [], 'total': 0}), [])
        with self.assertRaises(WebsiteSourceError):
            self.fetch({'products': []})

    def test_partial_or_invalid_catalog_fails(self):
        for data in [{'products': [], 'total': 1}, [], {'products': 'bad', 'total': 0},
                     {'products': [{'title': 'missing ID'}], 'total': 1},
                     {'products': [], 'total': 'invalid'}]:
            with self.subTest(data_type=type(data).__name__), self.assertRaises(WebsiteSourceError):
                self.fetch(data)

    def test_limits_and_repeated_pages_fail(self):
        data = catalog(); data['nextslice'] = '1'
        with self.assertRaises(WebsiteSourceError):
            self.fetch(data)
        data = catalog(); data['nextslice'] = '2'; data['total'] = 2
        source = k2_source(); source.max_pages = 1
        with self.assertRaisesRegex(WebsiteSourceError, 'max_pages'):
            self.fetch(data, source=source)
        source = k2_source(); source.max_details = 1
        with self.assertRaisesRegex(WebsiteSourceError, 'max_details'):
            self.fetch(data, source=source)

    def test_duplicate_products_fail(self):
        data = catalog(); data['products'] *= 2; data['total'] = 2
        with self.assertRaises(WebsiteSourceError):
            self.fetch(data)

    def test_off_host_links_are_not_requested(self):
        data = catalog(); data['products'][0]['url'] = 'https://other.example/job'
        with patch('app.collectors.k2.read_text', return_value=fixture('k2_listing.html')) as read, \
                patch('app.collectors.k2.read_json', return_value=data):
            with self.assertRaises(WebsiteSourceError):
                K2Collector(k2_source()).fetch()
        self.assertEqual(read.call_count, 1)

    def test_detail_failure_is_explicit_without_unverified_items(self):
        with patch('app.collectors.k2.read_text', side_effect=[fixture('k2_listing.html'), WebsiteSourceError('403')]), \
                patch('app.collectors.k2.read_json', return_value=catalog()):
            snapshot = K2Collector(k2_source()).fetch()
        self.assertEqual(snapshot,[])
        self.assertEqual(len(snapshot.failed_details),1)
        self.assertEqual(len(snapshot.catalog),1)
        snapshot = self.fetch(detail='<html>template changed</html>')
        self.assertEqual(snapshot,[])
        self.assertEqual(len(snapshot.failed_details),1)

    def test_changed_listing_fails_without_guessing(self):
        with patch('app.collectors.k2.read_text', return_value='<html>new page</html>'), patch('app.collectors.k2.read_json') as api:
            with self.assertRaises(WebsiteSourceError):
                K2Collector(k2_source()).fetch()
        api.assert_not_called()


class CrocTests(unittest.TestCase):
    def fetch(self, listing=None, detail=None, source=None):
        seen = []
        def read(url):
            seen.append(url)
            if len(seen) == 1:
                return fixture('croc_listing.html')
            if urlsplit(url).path == '/vacancies/':
                return listing if listing is not None else fixture('croc_filtered.html')
            return detail if detail is not None else fixture('croc_detail.html')
        with patch('app.collectors.croc.read_text', side_effect=read):
            return CrocCollector(source or croc_source()).fetch(), seen

    def test_live_filter_all_cards_and_full_descriptions(self):
        items, calls = self.fetch()
        self.assertEqual(len(items), 13)
        query = parse_qs(urlsplit(calls[1]).query)
        self.assertIn('EXPERIENCE', query)
        self.assertNotIn('_sft_expirience', query)
        self.assertEqual(len(calls), 15)  # discovery, filtered list, all 13 details
        self.assertEqual(len({i.external_id for i in items}), 13)
        for item in items:
            self.assertGreater(len(item.raw_text), 800)
            self.assertTrue(item.apply_url.endswith('#responseForm'))
            self.assertIn('0-1 год', item.raw_text)
            self.assertIsNone(item.published_at)
        self.assertTrue(any(i.external_id == 'stazher-analitik-soc' for i in items))
        self.assertTrue(any(i.external_id == 'stazher-dezhurnyy-inzhener-linux' for i in items))

    def test_reads_more_than_fifteen_details(self):
        soup = BeautifulSoup(fixture('croc_filtered.html'), 'html.parser')
        container = soup.select_one('.jsVacanciesListValues')
        template = container.select_one('.vacancy__card-item')
        for n in range(5):
            card = BeautifulSoup(str(template), 'html.parser').select_one('.vacancy__card-item'); card.select_one('a')['href'] = f'/vacancies/additional-{n}/';container.append(card)
        items, calls = self.fetch(listing=str(soup))
        self.assertEqual(len(items), 18)
        self.assertEqual(len(calls), 20)
        self.assertTrue(all(len(i.raw_text) > 800 for i in items))

    def test_server_must_apply_filter(self):
        html = fixture('croc_filtered.html').replace('selected=""', '').replace('selected', '')
        with self.assertRaisesRegex(WebsiteSourceError, 'фильтр'):
            self.fetch(listing=html)

    def test_missing_list_or_ambiguous_empty_list_fails(self):
        html = fixture('croc_filtered.html')
        with self.assertRaises(WebsiteSourceError):
            self.fetch(listing=html.replace('jsVacanciesListValues', 'changed'))
        soup = BeautifulSoup(html, 'html.parser');soup.select_one('.jsVacanciesListValues').clear()
        with self.assertRaises(WebsiteSourceError):
            self.fetch(listing=str(soup))
        soup.select_one('.jsVacanciesListValues').string = 'Вакансии не найдены'
        self.assertEqual(self.fetch(listing=str(soup))[0], [])

    def test_limits_reject_partial_catalog(self):
        source = croc_source(); source.max_details = 1
        with self.assertRaisesRegex(WebsiteSourceError, 'max_details'):
            self.fetch(source=source)
        source = croc_source(); source.max_pages = 1
        html = fixture('croc_filtered.html') + '<a href="?PAGEN_1=2">2</a>'
        with self.assertRaisesRegex(WebsiteSourceError, 'max_pages'):
            self.fetch(source=source, listing=html)

    def test_public_pagination_preserves_experience_filter(self):
        original = fixture('croc_filtered.html')
        second = BeautifulSoup(original, 'html.parser')
        container = second.select_one('.jsVacanciesListValues')
        card = BeautifulSoup(str(container.select_one('.vacancy__card-item')), 'html.parser').select_one('.vacancy__card-item');card.select_one('a')['href']='/vacancies/page-two/'
        container.clear();container.append(card)
        seen = []
        def read(url):
            seen.append(url)
            if len(seen) == 1:return fixture('croc_listing.html')
            if urlsplit(url).path != '/vacancies/':return fixture('croc_detail.html')
            query = parse_qs(urlsplit(url).query)
            self.assertIn('EXPERIENCE', query)
            return str(second) if query.get('PAGEN_1') == ['2'] else original+'<a href="?PAGEN_1=2">2</a>'
        with patch('app.collectors.croc.read_text', side_effect=read):
            items = CrocCollector(croc_source()).fetch()
        self.assertEqual(len(items), 14)
        self.assertTrue(any(i.external_id=='page-two' for i in items))

    def test_unknown_pagination_or_external_host_fails(self):
        for extra in ['<div class="pagination">Показать ещё</div>', '<a href="https://other.example/?PAGEN_1=2">2</a>']:
            with self.assertRaises(WebsiteSourceError):
                self.fetch(listing=fixture('croc_filtered.html')+extra)

    def test_repeated_page_is_not_a_complete_catalog(self):
        html = fixture('croc_filtered.html') + '<a href="?PAGEN_1=2">2</a>'
        with self.assertRaisesRegex(WebsiteSourceError, 'повторяет'):
            self.fetch(listing=html)

    def test_first_page_backlink_is_not_fetched_twice(self):
        html = fixture('croc_filtered.html') + '<a href="?PAGEN_1=1">1</a>'
        items, calls = self.fetch(listing=html)
        self.assertEqual(len(items), 13)
        self.assertEqual(len(calls), 15)

    def test_missing_detail_or_form(self):
        snapshot, _ = self.fetch(detail='<html>not found</html>')
        self.assertEqual(snapshot,[])
        self.assertEqual(len(snapshot.failed_details),13)
        items, _ = self.fetch(detail=fixture('croc_detail.html').replace('responseForm','noForm'))
        self.assertTrue(all(i.apply_url is None for i in items))


class PublicAccessTests(unittest.TestCase):
    def test_rtkit_reports_401_without_demo_or_auth_fallback(self):
        source = HttpSource('rtkit', 'RTKit', 'https://rtkit.ru/careers', 'rtkit_careers')
        with patch('app.collectors.rtkit.read_text', side_effect=[fixture('rtkit_shell.html'), WebsiteSourceError('HTTP 401')]) as read:
            with self.assertRaisesRegex(WebsiteSourceError, 'не подключён.*401'):
                RtkitCollector(source).fetch()
        self.assertEqual(read.call_count, 2)
        read.assert_called_with('https://rtkit.ru/api/v1/vacancies', post=True)

    def test_rtkit_does_not_trust_unverified_new_api_response(self):
        source = HttpSource('rtkit', 'RTKit', 'https://rtkit.ru/careers', 'rtkit_careers')
        with patch('app.collectors.rtkit.read_text', side_effect=[fixture('rtkit_shell.html'), '{"items":[]}']):
            with self.assertRaisesRegex(WebsiteSourceError, 'не проверена'):
                RtkitCollector(source).fetch()

    def test_http_errors_do_not_leak_request_details(self):
        for status in [401, 403, 429, 500]:
            response = requests.Response();response.status_code=status
            with patch('app.collectors.website_http.http_get', side_effect=requests.HTTPError('private input',response=response)):
                with self.assertRaises(WebsiteSourceError) as caught:
                    read_text('https://example.org/public')
            self.assertIn(str(status),str(caught.exception))
            self.assertNotIn('private input',str(caught.exception))
        with patch('app.collectors.website_http.http_get', side_effect=requests.Timeout('private input')):
            with self.assertRaisesRegex(WebsiteSourceError, 'Timeout'):
                read_text('https://example.org/public')

    def test_redirect_and_access_challenge_are_not_followed(self):
        for status, content in [(302, b''), (200, b'<title>Access Denied</title>')]:
            response = requests.Response();response.status_code=status;response._content=content
            with patch('app.collectors.website_http.http_get',return_value=response) as get:
                with self.assertRaises(WebsiteSourceError):read_text('https://example.org/')
            get.assert_called_once_with('https://example.org/',allow_redirects=False)

    def test_invalid_json_and_job_dates(self):
        with patch('app.collectors.website_http.read_text',return_value='not json'):
            with self.assertRaises(WebsiteSourceError):read_json('https://example.org/')
        with self.assertRaises(WebsiteSourceError):
            job_dates(BeautifulSoup('<script type="application/ld+json">{"@type":"JobPosting","validThrough":"wrong"}</script>','html.parser'))


class WebsiteIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.cfg = Config.load(str(ROOT / 'config.yaml'))
        self.cfg.db_path = ':memory:'
        self.cfg.owner_id = 123
        self.cfg.channels = []
        self.cfg.websites = [k2_source()]
        self.bot = Bot(self.cfg)
        self.addCleanup(self.bot.db.close)
        self.bot.notifier.send = Mock(return_value=True)

    def test_failure_does_not_establish_baseline_and_next_success_clears_error(self):
        with patch.object(K2Collector,'fetch',side_effect=WebsiteSourceError('offline')):
            self.bot.run_cycle()
        self.assertFalse(self.bot.db.source_initialized('k2'))
        self.assertEqual(self.bot.db.get_source_error('k2'),'offline')
        with patch.object(K2Collector,'fetch',return_value=[]):self.bot.run_cycle()
        self.assertTrue(self.bot.db.source_initialized('k2'))
        self.assertFalse(self.bot.db.get_source_error('k2'))

    def test_first_success_does_not_send_archive_after_start(self):
        self.bot.set_enabled(True)
        item = Item('k2','K2','intern','https://example.org/intern','Стажировка Python','','',None)
        with patch.object(K2Collector,'fetch',return_value=[item]):self.bot.run_cycle()
        self.assertTrue(self.bot.db.exists('k2:intern'))
        self.bot.notifier.send.assert_not_called()

    def test_failed_site_does_not_block_other_sites(self):
        self.cfg.websites.append(croc_source())
        with patch.object(K2Collector,'fetch',side_effect=WebsiteSourceError('offline')), patch.object(CrocCollector,'fetch',return_value=[]):
            self.bot.run_cycle()
        self.assertTrue(self.bot.db.source_initialized('croc'))
        self.assertIn('k2',self.bot.db.last_run()['errors'])

    def test_old_rtkit_demo_queue_is_preserved_but_not_sent(self):
        self.cfg.websites = [HttpSource('rtkit','RTKit','https://rtkit.ru/careers','rtkit_careers')]
        old = Item('rtkit','RTKit','vacancy:architect-100001','https://rtkit.ru/careers/architect-100001','Системный архитектор','',None,None)
        self.bot.set_enabled(True)
        self.bot.db.add_item(old,category='career')
        with patch.object(RtkitCollector,'fetch',side_effect=WebsiteSourceError('HTTP 401')):
            self.bot.run_cycle()
        self.bot.notifier.send.assert_not_called()
        self.assertTrue(self.bot.db.exists(old.item_key))
        self.assertEqual(self.bot.db.user_pending(datetime.now(timezone.utc)),[])
        self.assertEqual(self.bot.db.item_row(old.item_key)['delivery_state'],'pending')
        self.assertIsNone(self.bot.db.user_delivery(123,old.item_key))


if __name__ == '__main__':
    unittest.main()
