"""CROC: current EXPERIENCE filter, paginated catalogue, complete detail sections."""
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from app.collectors.website_http import WebsiteSourceError, read_text, text, same_host_url, job_dates
from app.models import Item


class CrocCollector:
    type_key = 'croc_vacancies'
    HOST = 'careers.croc.ru'

    def __init__(self, source):
        self.source = source

    def fetch(self) -> list[Item]:
        source_url = same_host_url(self.source.url, self.source.url, self.HOST)
        soup = BeautifulSoup(read_text(source_url), 'html.parser')
        experience = soup.select_one('select[name="EXPERIENCE"]')
        if experience is None or soup.select_one('.jsVacanciesWrapper') is None:
            raise WebsiteSourceError('КРОК: действующий фильтр опыта не найден.')
        value = next((o.get('value') for o in experience.select('option') if text(o) == '0-1 год'), None)
        if not value:
            raise WebsiteSourceError('КРОК: вариант опыта «0-1 год» не найден.')
        parsed = urlsplit(source_url)
        query = [(k, v) for k, v in parse_qsl(parsed.query)
                 if k not in ('_sft_expirience', 'EXPERIENCE') and not k.startswith('PAGEN_')]
        first = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(sorted([*query, ('EXPERIENCE', value)])), ''))
        pending, visited, cards = [first], set(), {}
        page_contents = set()
        while pending:
            url = pending.pop(0)
            if url in visited:
                continue
            if len(visited) >= self.source.max_pages:
                raise WebsiteSourceError('КРОК: достигнут max_pages; частичная выдача отклонена.')
            visited.add(url)
            listing = BeautifulSoup(read_text(url), 'html.parser')
            selected = listing.select_one('select[name="EXPERIENCE"] option[selected]')
            if selected is None or selected.get('value') != value:
                raise WebsiteSourceError('КРОК: сервер не подтвердил выбранный фильтр опыта.')
            container = listing.select_one('.jsVacanciesSorted .jsVacanciesListValues')
            if container is None:
                raise WebsiteSourceError('КРОК: отсутствует полный отфильтрованный список.')
            nodes = container.select('.vacancy__card-item > a[href]')
            if not nodes and not re.search(r'(?:вакансии|вакансий)\s+не найден|ничего не найден|нет вакансий', text(container), re.I):
                raise WebsiteSourceError('КРОК: пустая выдача без явного сообщения об отсутствии вакансий.')
            signature = tuple(sorted(node['href'] for node in nodes))
            if nodes and signature in page_contents:
                raise WebsiteSourceError('КРОК: сервер повторяет страницу; полнота не подтверждена.')
            page_contents.add(signature)
            for node in nodes:
                detail_url = same_host_url(node['href'], source_url, self.HOST)
                if not re.fullmatch(r'/vacancies/[^/]+/', urlsplit(detail_url).path):
                    raise WebsiteSourceError('КРОК: неожиданная ссылка вакансии.')
                cards[detail_url] = text(node)
            if len(cards) > self.source.max_details:
                raise WebsiteSourceError('КРОК: достигнут max_details; частичная выдача отклонена.')
            pagers = listing.select('.pagination, .jsPagination, .pagination-block, .modern-page-navigation')
            links = listing.select('a[href*="PAGEN_"]')
            if pagers and not links and any(re.search(r'следующ|показать ещ[её]|загрузить ещ[её]', text(p), re.I) for p in pagers):
                raise WebsiteSourceError('КРОК: пагинация без публичных ссылок; полнота не подтверждена.')
            for link in links:
                target = same_host_url(link['href'], url, self.HOST)
                parts = urlsplit(target)
                if parts.path != parsed.path:
                    raise WebsiteSourceError('КРОК: неожиданная страница пагинации.')
                params = dict(parse_qsl(parts.query))
                params['EXPERIENCE'] = value
                params.pop('_sft_expirience', None)
                for key in list(params):
                    if key.startswith('PAGEN_'):
                        if not params[key].isdigit() or int(params[key]) < 1:
                            raise WebsiteSourceError('КРОК: некорректный номер страницы.')
                        if int(params[key]) == 1:
                            del params[key]
                target = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(sorted(params.items())), ''))
                if target not in visited and target not in pending:
                    pending.append(target)
        items = []
        for url in cards:
            soup = BeautifulSoup(read_text(url), 'html.parser')
            main = soup.select_one('.vacancy-detail__content-main')
            title = main.select_one('h1') if main else None
            sections = main.select('.vacancy-detail__content-main-text-block, .vacancy-detail__content-main-item') if main else []
            if title is None or len(sections) < 2:
                raise WebsiteSourceError('КРОК: описание вакансии отсутствует или неполное.')
            tags = main.find('ul', recursive=False)
            content = '\n'.join(filter(None, [text(title), text(tags) if tags else '', *(text(s) for s in sections)]))
            published, deadline = job_dates(soup)
            items.append(Item(
                source_key=self.source.key, source_name=self.source.name,
                external_id=urlsplit(url).path.rstrip('/').rsplit('/', 1)[-1],
                url=url, title=text(title), summary=content[:500], raw_text=content,
                apply_url=url + '#responseForm' if soup.select_one('#responseForm form') else None,
                published_at=published, deadline=deadline, registration_status='unknown',
            ))
        return items
