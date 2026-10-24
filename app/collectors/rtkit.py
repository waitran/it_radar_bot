"""RTKit is explicitly unconnected: its live API requires authorization.

The JavaScript in-memory collections are disabled demo fixtures, not vacancies.
Never read those collections or use embedded credentials to bypass API access.
"""
from bs4 import BeautifulSoup

from app.collectors.base import check_collection, collection_task
from app.collectors.website_http import WebsiteSourceError, read_text, same_host_url


class RtkitCollector:
    type_key = 'rtkit_careers'
    API = 'https://rtkit.ru/api/v1/vacancies'

    def __init__(self, source):
        self.source = source

    @collection_task
    def fetch(self):
        url = same_host_url(self.source.url, self.source.url, 'rtkit.ru')
        if BeautifulSoup(read_text(url), 'html.parser').select_one('app-root') is None:
            raise WebsiteSourceError('РТК ИТ не подключён: изменилась публичная страница, нужна проверка интеграции.')
        check_collection()
        try:
            read_text(self.API, post=True)
        except WebsiteSourceError as exc:
            raise WebsiteSourceError(f'РТК ИТ не подключён: публичный API недоступен. {exc}') from None
        raise WebsiteSourceError('РТК ИТ: доступ API изменился; интеграция данных ещё не проверена. Демоданные не используются.')
