"""Конфигурация бота: переменные окружения + config.yaml (источники и правила отбора)."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any

import yaml


@dataclass
class TelegramChannelSource:
    key: str
    username: str  # без @, например rostelecom_tech
    type: str = "telegram_channel"

    @property
    def web_url(self) -> str:
        return f"https://t.me/s/{self.username}"

    @property
    def display_name(self) -> str:
        return f"Telegram-канал t.me/{self.username}"


@dataclass
class HttpSource:
    key: str
    name: str
    url: str
    type: str  # generic http collector key: croc_vacancies | k2_vacancy | rtkit_careers


@dataclass
class Rules:
    """Настраиваемые категории отбора."""
    categories: dict[str, dict[str, Any]] = field(default_factory=dict)
    exclude_keywords: list[str] = field(default_factory=list)
    min_score: int = 1
    max_age_days: int = 60
    closed_words: list[str] = field(default_factory=lambda: ["заверши", "подвели итоги", "отключена"])


def _norm(s: str) -> str:
    s = s.lower().replace("ё", "е")
    return re.sub(r"\s+", " ", s)


@dataclass
class Config:
    bot_token: str
    owner_id: int
    db_path: str
    poll_interval_seconds: int
    request_timeout_seconds: int
    user_agent: str
    channels: list[TelegramChannelSource]
    websites: list[HttpSource]
    rules: Rules
    first_run_lookback_hours: int = 72

    @staticmethod
    def load(path: str | None = None) -> "Config":
        path = path or os.environ.get("CONFIG_PATH", "config.yaml")
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}

        channels = [
            TelegramChannelSource(key=ch["key"], username=ch["username"].lstrip("@"))
            for ch in raw.get("telegram_channels", [])
        ]
        websites = [
            HttpSource(key=w["key"], name=w["name"], url=w["url"], type=w["type"])
            for w in raw.get("websites", [])
        ]
        rules_raw = raw.get("rules", {})
        categories = {}
        for cat_key, cat in (rules_raw.get("categories") or {}).items():
            categories[_norm(cat_key)] = {
                "label": cat.get("label", cat_key),
                "keywords": [_norm(k) for k in cat.get("keywords", [])],
            }
        rules = Rules(
            categories=categories,
            exclude_keywords=[_norm(k) for k in rules_raw.get("exclude_keywords", [])],
            min_score=int(rules_raw.get("min_score", 1)),
            max_age_days=int(rules_raw.get("max_age_days", 60)),
            closed_words=[_norm(w) for w in rules_raw.get("closed_words", ["заверши", "подвели итоги", "отключена"])],
        )
        return Config(
            bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
            owner_id=int(os.environ.get("OWNER_TELEGRAM_ID", "0")),
            db_path=os.environ.get("DB_PATH", "/data/bot.db"),
            poll_interval_seconds=int(os.environ.get("POLL_INTERVAL_SECONDS", "3600")),
            request_timeout_seconds=int(os.environ.get("REQUEST_TIMEOUT_SECONDS", "30")),
            user_agent=os.environ.get(
                "USER_AGENT",
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
            ),
            channels=channels,
            websites=websites,
            rules=rules,
            first_run_lookback_hours=int(os.environ.get("FIRST_RUN_LOOKBACK_HOURS", "72")),
        )
