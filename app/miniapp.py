"""Mini App launch configuration and keyboard; no network or database access."""
from urllib.parse import urlsplit


def validate_url(value):
    value = value.strip()
    if not value:
        return ''
    try:
        url = urlsplit(value)
        valid = (len(value) <= 2048 and url.scheme == 'https' and url.hostname
                 and not url.username and not url.password and not url.fragment
                 and not any(c.isspace() or ord(c) < 32 for c in value))
        _ = url.port
    except ValueError:
        valid = False
    if not valid:
        raise ValueError('MINIAPP_URL must be a valid HTTPS URL without credentials or fragment')
    return value


def launch_keyboard(url):
    if not url:
        return None
    return {'inline_keyboard': [[{'text': '📱 Открыть IT Radar', 'web_app': {'url': validate_url(url)}}]]}
