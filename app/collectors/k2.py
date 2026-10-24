"""K2: public Tilda catalogue advertised by the page, then complete vacancy pages."""
import re
from urllib.parse import urlencode, urlsplit

from bs4 import BeautifulSoup

from app.collectors.base import check_collection, collection_task
from app.collectors.website_http import WebsiteSourceError, read_text, read_json, text, same_host_url, job_dates
from app.models import Item


class K2Collector:
    type_key = 'k2_vacancy'
    HOST = 'careers.cybersecurity.k2.tech'
    API = 'https://store.tildaapi.com/api/getproductslist/'
    CONTENT_TYPES = {'1060', '491', '106', '795', '508', '1095'}

    def __init__(self, source):
        self.source = source

    @collection_task
    def fetch(self) -> list[Item]:
        url = same_host_url(self.source.url, self.source.url, self.HOST)
        html = read_text(url)
        soup = BeautifulSoup(html, 'html.parser')
        options = re.search(r"var\s+options\s*=\s*\{recid:['\"](\d+)['\"],storepart:['\"](\d+)['\"]", html)
        if not options or soup.select_one('.js-store') is None:
            raise WebsiteSourceError('К2: конфигурация публичного каталога не найдена; изменилась разметка.')
        recid, storepart = options.groups()
        page, seen_pages, products = '1', set(), {}
        total = None
        while page:
            check_collection()
            if page in seen_pages or len(seen_pages) >= self.source.max_pages:
                raise WebsiteSourceError('К2: повтор страницы или лимит max_pages; частичная выдача отклонена.')
            seen_pages.add(page)
            data = read_json(self.API + '?' + urlencode({
                'storepartuid': storepart, 'recid': recid, 'getparts': 'true', 'getoptions': 'true',
                'slice': page, 'size': '100', 'flag_root': 'withroot',
            }))
            if not isinstance(data, dict) or not isinstance(data.get('products'), list):
                raise WebsiteSourceError('К2: неверный формат публичного каталога.')
            try:
                current_total = int(data['total'])
                if current_total < 0 or total is not None and current_total != total:
                    raise ValueError()
            except (KeyError, TypeError, ValueError):
                raise WebsiteSourceError('К2: отсутствует или меняется размер каталога; полнота не подтверждена.') from None
            total = current_total
            if total > self.source.max_details:
                raise WebsiteSourceError('К2: превышен max_details; частичная выдача отклонена.')
            for product in data['products']:
                if not isinstance(product, dict) or not product.get('uid') or not product.get('title'):
                    raise WebsiteSourceError('К2: неполная запись каталога.')
                identifier = str(product['uid'])
                if identifier in products:
                    raise WebsiteSourceError('К2: повтор ID каталога; полнота не подтверждена.')
                products[identifier] = product
            page = str(data['nextslice']) if data.get('nextslice') else ''
        if len(products) != total:
            raise WebsiteSourceError('К2: получен неполный каталог вакансий.')
        items, seen_urls = [], set()
        for product in products.values():
            check_collection()
            target = product.get('url') or product.get('buttonlink')
            if not target:
                raise WebsiteSourceError('К2: отсутствует ссылка вакансии.')
            url = same_host_url(target, self.source.url, self.HOST)
            path = urlsplit(url).path.rstrip('/')
            if path in ('', '/vacancy') or url in seen_urls:
                raise WebsiteSourceError('К2: некорректная или повторная ссылка вакансии.')
            seen_urls.add(url)
            detail = BeautifulSoup(read_text(url), 'html.parser')
            if detail.select_one('.t1060__title') is None:
                raise WebsiteSourceError('К2: не найден заголовок страницы вакансии.')
            blocks = [block for block in detail.select('#allrecords > .t-rec')
                      if block.get('data-record-type') in self.CONTENT_TYPES]
            content = list(dict.fromkeys(text(block) for block in blocks if text(block)))
            if len(content) < 3:
                raise WebsiteSourceError('К2: описание вакансии неполное.')
            characteristics = product.get('characteristics', [])
            if not isinstance(characteristics, list):
                raise WebsiteSourceError('К2: некорректные характеристики вакансии.')
            tags = [text(x.get('value')) for x in characteristics if isinstance(x, dict)]
            full_text = '\n'.join(dict.fromkeys(filter(None, [text(product.get('descr')), *tags, *content])))
            published, deadline = job_dates(detail)
            apply_url = url + '#form' if detail.select_one('a[name="form"], #form') and detail.select_one('form') else None
            items.append(Item(
                source_key=self.source.key, source_name=self.source.name,
                # Preserve the former sitemap collector's slug IDs and seen rows.
                external_id=path.rsplit('/', 1)[-1], url=url, title=text(product['title']),
                summary=full_text[:500], raw_text=full_text, apply_url=apply_url,
                published_at=published, deadline=deadline, registration_status='unknown',
                kind='vacancy',
            ))
        check_collection()
        return items
