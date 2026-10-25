"""Telegram Bot API: one request per send, explicit uncertain delivery outcome."""
from dataclasses import dataclass
import html

import requests

API = 'https://api.telegram.org/bot{token}/{method}'


class BotAPIError(RuntimeError):
    def __init__(self, code=0, retry_after=0):
        super().__init__(f'Telegram API error {code}')
        self.code, self.retry_after = code, retry_after


@dataclass(frozen=True)
class DeliveryResult:
    status: str  # sent | retry | uncertain | failed
    code: int = 0
    retry_after: int = 0
    message_id: int | None = None


def _retry_after(data):
    try:
        value = int(data.get('parameters', {}).get('retry_after', 60))
        return value if 0 < value <= 604800 else 604800
    except (TypeError, ValueError, AttributeError):
        return 60


class TelegramNotifier:
    def __init__(self, bot_token: str, owner_id: int = 0):
        self.bot_token, self.owner_id = bot_token, owner_id

    def send(self, text: str, chat_id: int | None = None, *, reply_markup=None) -> DeliveryResult:
        chat_id = self.owner_id if chat_id is None else chat_id
        if not self.bot_token or chat_id <= 0:
            return DeliveryResult('failed', code=401)
        try:
            response = requests.post(API.format(token=self.bot_token, method='sendMessage'),
                json={'chat_id': chat_id, 'text': text, 'parse_mode': 'HTML',
                      'disable_web_page_preview': True, **({'reply_markup': reply_markup} if reply_markup else {})}, timeout=(10,30), allow_redirects=False)
        except requests.ConnectTimeout:
            return DeliveryResult('retry', retry_after=10)
        except requests.RequestException:
            # A lost response is not proof that Telegram did not deliver the text.
            return DeliveryResult('uncertain')
        if response.status_code >= 500 or 300 <= response.status_code < 400:
            return DeliveryResult('uncertain', code=response.status_code)
        try:
            data = response.json()
        except ValueError:
            return DeliveryResult('retry', code=429, retry_after=60) if response.status_code == 429 else DeliveryResult('uncertain')
        if not isinstance(data, dict):
            return DeliveryResult('uncertain')
        result = data.get('result')
        if response.status_code == 200 and data.get('ok') is True and isinstance(result, dict) and type(result.get('message_id')) is int:
            return DeliveryResult('sent', message_id=result['message_id'])
        code = data.get('error_code', response.status_code)
        if code == 429:
            return DeliveryResult('retry', code=429, retry_after=_retry_after(data))
        if data.get('ok') is False and type(code) is int and 400 <= code < 500:
            return DeliveryResult('failed', code=code)
        return DeliveryResult('uncertain', code=response.status_code)

    def call(self, method, payload=None, *, timeout=30):
        try:
            response = requests.post(API.format(token=self.bot_token, method=method),
                                     json=payload or {}, timeout=(10, timeout), allow_redirects=False)
            data = response.json()
        except (requests.RequestException, ValueError):
            raise BotAPIError() from None
        if not isinstance(data, dict):
            raise BotAPIError()
        if response.status_code == 200 and data.get('ok') is True:
            return data.get('result')
        code = data.get('error_code', response.status_code)
        raise BotAPIError(code if type(code) is int else 0, _retry_after(data) if code == 429 else 0)

    @staticmethod
    def escape(value):
        return html.escape(value or '', quote=True)
