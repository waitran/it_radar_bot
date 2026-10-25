"""Server-only configuration. No authentication bypass exists."""
import os
import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Settings:
    db_path: str = '/data/bot.db'
    bot_token: str = field(default='', repr=False)
    auth_ttl: int = 3600
    user_db_path: str | None = None
    user_rate_limit: int = 120
    global_rate_limit: int = 600

    def __post_init__(self):
        if not self.bot_token or not 1 <= self.auth_ttl <= 86400:
            raise ValueError('Bot token and auth TTL (1..86400) are required')
        if not 1 <= self.user_rate_limit <= 10000 or not 1 <= self.global_rate_limit <= 100000:
            raise ValueError('Invalid API rate limits')

    @classmethod
    def from_env(cls):
        token = os.environ.get('TELEGRAM_BOT_TOKEN', '').strip()
        if not re.fullmatch(r'[0-9]+:[A-Za-z0-9_-]{20,}', token):
            raise ValueError('Set a valid server-side TELEGRAM_BOT_TOKEN')
        try:
            return cls(os.environ.get('MINIAPP_DB_PATH', '/data/bot.db'), token,
                       int(os.environ.get('MINIAPP_AUTH_TTL', '3600')),
                       os.environ.get('MINIAPP_USER_DB_PATH'),
                       int(os.environ.get('MINIAPP_USER_RATE_LIMIT', '120')),
                       int(os.environ.get('MINIAPP_GLOBAL_RATE_LIMIT', '600')))
        except (TypeError, ValueError):
            raise ValueError('Invalid Mini App server configuration') from None
