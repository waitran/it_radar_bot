"""Базовые утилиты сборщиков: HTTP-клиент с разумной частотой запросов."""
from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
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


class CollectionCancelled(RuntimeError):
    """A shutdown interrupted collection; a partial snapshot must not be committed."""


class CollectionTimeout(RuntimeError):
    """The source collection budget elapsed."""


_control = ContextVar('collection_control', default=None)


def check_collection():
    control = _control.get()
    if control is None:
        return float('inf')
    stop, deadline = control
    if stop is not None and stop.is_set():
        raise CollectionCancelled('Сбор отменён при завершении процесса')
    remaining = deadline-time.monotonic()
    if remaining <= 0:
        raise CollectionTimeout('Истёк бюджет времени сбора источника')
    return remaining


@contextmanager
def collection_scope(stop=None, *, budget=600):
    # Nested collector/helper calls share the outer source's deadline.
    if _control.get() is not None:
        check_collection()
        yield
        check_collection()
        return
    token = _control.set((stop,time.monotonic()+budget))
    try:
        check_collection()
        yield
        check_collection()
    finally:
        _control.reset(token)


def collection_task(function):
    @wraps(function)
    def bounded(*args, **kwargs):
        with collection_scope():
            return function(*args, **kwargs)
    return bounded


def _pace():
    remaining = check_collection()
    wait = max(0,_MIN_INTERVAL-(time.monotonic()-_last_request_at))
    control = _control.get()
    delay = min(wait,remaining)
    if delay:
        if control is not None and control[0] is not None:
            control[0].wait(delay)
        else:
            time.sleep(delay)
    check_collection()


def http_get(url: str, *, timeout: int = 25, headers: dict | None = None,
             allow_redirects: bool = True) -> requests.Response:
    global _last_request_at
    _pace()
    # requests applies its timeout separately to connect/read; bound both against
    # the remaining source budget. An in-flight request still uses network timeouts.
    timeout = min(timeout,check_collection()/2)
    h = {"User-Agent": USER_AGENT, "Accept-Language": "ru,en;q=0.8"}
    if headers:
        h.update(headers)
    resp = None
    try:
        resp = requests.get(url, timeout=timeout, headers=h, allow_redirects=allow_redirects)
        check_collection()
    except (CollectionCancelled,CollectionTimeout):
        if resp is not None:
            resp.close()
        raise
    finally:
        _last_request_at = time.monotonic()
    resp.raise_for_status()
    return resp


def http_post_json(url: str, payload: dict, *, timeout: int = 25) -> requests.Response:
    """Public read APIs that use POST; shares pacing and cancellation with GET."""
    global _last_request_at
    _pace()
    timeout = min(timeout,check_collection()/2)
    resp = None
    try:
        resp = requests.post(url, json=payload, timeout=timeout,
                             headers={"User-Agent": USER_AGENT}, allow_redirects=False)
        check_collection()
    except (CollectionCancelled,CollectionTimeout):
        if resp is not None:
            resp.close()
        raise
    finally:
        _last_request_at = time.monotonic()
    resp.raise_for_status()
    return resp
