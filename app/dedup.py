"""Conservative identities: specific URLs, or identical content within a source."""
import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def canonical_url(value):
    try:
        p = urlsplit(value or '')
        if p.scheme not in {'http', 'https'} or not p.hostname or p.username or p.password:
            return None
        # Generic landing pages are not reliable identities for an opportunity.
        if p.path in ('', '/'):
            return None
        query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
                 if not k.lower().startswith('utm_') and k.lower() not in {'fbclid','gclid','yclid'}]
        return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path.rstrip('/'), urlencode(sorted(query)), ''))
    except ValueError:
        return None


def identities(item):
    keys = set()
    for value in (item.url, item.apply_url):
        url = canonical_url(value)
        if url:
            keys.add('url:' + url)
    if item.raw_text.strip():
        content = re.sub(r'\s+', ' ', item.title + '\n' + item.raw_text).strip().casefold()
        keys.add('text:' + item.source_key + ':' + hashlib.sha256(content.encode()).hexdigest())
    return sorted(keys)

