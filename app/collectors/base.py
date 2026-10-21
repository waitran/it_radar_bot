"""Базовые утилиты сборщиков: HTTP-клиент с разумной частотой запросов."""
from __future__ import annotations

import logging
import time

import requests

log = logging.getLogger("collectors")

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36 it-radar-bot/1.0"
)

# Между запросами к любому источнику выдерживаем паузу — соблюдаем разумную частоту.
_MIN_INTERVAL = 2.0
_last_request_at = 0.0


def http_get(url: str, *, timeout: int = 25, headers: dict | None = None) -> requests.Response:
    global _last_request_at
    wait = _MIN_INTERVAL - (time.monotonic() - _last_request_at)
    if wait > 0:
        time.sleep(wait)
    h = {"User-Agent": USER_AGENT, "Accept-Language": "ru,en;q=0.8"}
    if headers:
        h.update(headers)
    resp = requests.get(url, timeout=timeout, headers=h)
    _last_request_at = time.monotonic()
    resp.raise_for_status()
    return resp
