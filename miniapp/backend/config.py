"""Server-only configuration. No authentication bypass exists."""
import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Settings:
    db_path: str = '/data/bot.db'
    bot_token: str = field(default='', repr=False)
    auth_ttl: int = 3600
    user_db_path: str | None = None

    def __post_init__(self):
        if not self.bot_token or not 1 <= self.auth_ttl <= 86400:
            raise ValueError('Bot token and auth TTL (1..86400) are required')

    @classmethod
    def from_env(cls):
        return cls(os.environ.get('MINIAPP_DB_PATH', '/data/bot.db'),
                   os.environ.get('TELEGRAM_BOT_TOKEN', ''),
                   int(os.environ.get('MINIAPP_AUTH_TTL', '3600')),
                   os.environ.get('MINIAPP_USER_DB_PATH'))
