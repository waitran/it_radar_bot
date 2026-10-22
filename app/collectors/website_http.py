"""Public website requests and parsing helpers; no credentials or access bypass."""
import json
import re
from datetime import datetime, time, timedelta, timezone
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

from app.collectors.base import http_get, http_post_json


class WebsiteSourceError(RuntimeError):
    pass


def same_host_url(value, base, host):
    value = urljoin(base, str(value or ''))
    parsed = urlsplit(value)
    if (parsed.scheme != 'https' or parsed.netloc != host or parsed.fragment
            or re.search(r'[\s<>"\x00-\x1f]', value)):
        raise WebsiteSourceError('Неожиданный адрес страницы; переход не выполнялся.')
    return value


def read_text(url, *, post=False):
    try:
        response = http_post_json(url, {}) if post else http_get(url, allow_redirects=False)
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else 'unknown'
        raise WebsiteSourceError(f'HTTP {status} у {urlsplit(url).hostname}; обход ограничений не выполнялся.') from None
    except requests.RequestException as exc:
        raise WebsiteSourceError(f'{type(exc).__name__} у {urlsplit(url).hostname}.') from None
    if response.status_code != 200:
        raise WebsiteSourceError(f'HTTP {response.status_code} у {urlsplit(url).hostname}; перенаправления не выполняются.')
    if len(response.content) > 12_000_000:
        raise WebsiteSourceError('Ответ сайта превышает 12 МБ.')
    text = response.text
    if any(marker in text.lower() for marker in ('cf-chl-platform', '<title>access denied', '<title>captcha', 'just a moment...</title>')):
        raise WebsiteSourceError('Сайт ограничил автоматический доступ; обход не выполнялся.')
    return text


def read_json(url):
    try:
        return json.loads(read_text(url))
    except ValueError:
        raise WebsiteSourceError('Сайт вернул некорректный JSON.') from None


def text(node):
    if not hasattr(node, 'get_text'):
        node = BeautifulSoup(str(node or ''), 'html.parser')
    return ' '.join(node.get_text(' ', strip=True).split())


def job_dates(soup):
    """Use explicitly published JobPosting dates only, never sitemap lastmod."""
    def parse(value, end_of_day=False):
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
            if len(str(value)) == 10 and end_of_day:
                parsed = datetime.combine(parsed.date(), time.max)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone(timedelta(hours=3)))
        except ValueError:
            raise WebsiteSourceError('Некорректная дата в JobPosting.') from None
    for node in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(node.string or node.get_text())
        except (ValueError, TypeError):
            continue
        pending = data if isinstance(data, list) else [data]
        while pending:
            item = pending.pop()
            if not isinstance(item, dict):
                continue
            if isinstance(item.get('@graph'), list):
                pending.extend(item['@graph'])
            kind = item.get('@type')
            if kind == 'JobPosting' or isinstance(kind, list) and 'JobPosting' in kind:
                return parse(item.get('datePosted')), parse(item.get('validThrough'), True)
    return None, None
