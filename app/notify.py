"""Доставка уведомлений через Telegram Bot API (sendMessage).

Только владельцу, указанному в настройках (OWNER_TELEGRAM_ID).
"""
from __future__ import annotations

import logging
import time

import requests

log = logging.getLogger("notify")

API = "https://api.telegram.org/bot{token}/{method}"


class TelegramNotifier:
    def __init__(self, bot_token: str, owner_id: int):
        self.bot_token = bot_token
        self.owner_id = owner_id

    def send(self, text: str) -> bool:
        """Отправка одного сообщения. Возвращает True при успехе."""
        if not self.bot_token or not self.owner_id:
            log.error("Не заданы TELEGRAM_BOT_TOKEN или OWNER_TELEGRAM_ID — сообщение не отправлено")
            return False
        payload = {
            "chat_id": self.owner_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        for attempt in range(3):
            try:
                resp = requests.post(API.format(token=self.bot_token, method="sendMessage"),
                                     json=payload, timeout=30)
                data = resp.json()
                if data.get("ok"):
                    return True
                desc = data.get("description", "")
                log.warning("sendMessage failed (attempt %d): %s %s", attempt + 1, resp.status_code, desc)
                # 429 — протестуем retry_after
                if resp.status_code == 429 and isinstance(data.get("parameters"), dict):
                    time.sleep(int(data["parameters"].get("retry_after", 5)) + 1)
                    continue
                if "bot was blocked" in desc or "chat not found" in desc:
                    log.error("Владелец не принимает сообщения: %s", desc)
                    return False
            except (requests.RequestException, ValueError) as e:
                log.warning("sendMessage error (attempt %d): %s", attempt + 1, e)
                time.sleep(2 * (attempt + 1))
        return False

    @staticmethod
    def escape(s: str) -> str:
        return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
