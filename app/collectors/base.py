"""Базовые утилиты сборщиков: HTTP-клиент с разумной частотой запросов."""
from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import time
import zlib

import requests
from urllib3.exceptions import HTTPError as UrllibHTTPError

from app.config import DEFAULT_REQUEST_TIMEOUT, DEFAULT_USER_AGENT

log = logging.getLogger("collectors")

# Между запросами к любому источнику выдерживаем паузу — соблюдаем разумную частоту.
_MIN_INTERVAL = 2.0
_last_request_at = 0.0


class CollectionCancelled(RuntimeError):
    """A shutdown interrupted collection; a partial snapshot must not be committed."""


class CollectionTimeout(RuntimeError):
    """The source collection budget elapsed."""


_control = ContextVar('collection_control', default=None)
_http_settings = ContextVar('collection_http_settings', default=(DEFAULT_REQUEST_TIMEOUT, DEFAULT_USER_AGENT))


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
def collection_scope(stop=None, *, budget=600, request_timeout=None, user_agent=None):
    # Nested collector/helper calls share the outer source's deadline.
    if _control.get() is not None:
        check_collection()
        yield
        check_collection()
        return
    token = _control.set((stop,time.monotonic()+budget))
    current_timeout, current_agent = _http_settings.get()
    settings_token = _http_settings.set((
        current_timeout if request_timeout is None else request_timeout,
        current_agent if user_agent is None else user_agent))
    try:
        check_collection()
        yield
        check_collection()
    finally:
        _http_settings.reset(settings_token)
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


class ResponseLimitError(requests.RequestException):
    """An untrusted source response cannot be safely buffered."""


def _chunks(raw):
    # urllib3 2.x read1 returns available data without waiting to fill 8 KiB.
    # Older runtimes use stream; both retain the socket read timeout.
    if hasattr(type(raw), 'read1'):
        while True:
            check_collection()
            chunk = raw.read1(8192, decode_content=False)
            if not chunk:
                return
            yield chunk
    else:
        yield from raw.stream(8192, decode_content=False)


def _read_response(response, max_bytes):
    # Read encoded chunks ourselves: automatic decompression may allocate an
    # arbitrarily large output before iter_content yields its first chunk.
    try:
        check_collection()
        response.raise_for_status()
        if response.status_code != 200:
            # Leave the body unconsumed so close() discards this connection.
            return response
        encoding = response.headers.get('Content-Encoding', 'identity').lower().strip()
        if encoding not in ('identity', '', 'gzip', 'deflate'):
            raise ResponseLimitError('Неподдерживаемое сжатие ответа источника')
        decoder = (zlib.decompressobj(31 if encoding == 'gzip' else zlib.MAX_WBITS)
                   if encoding in ('gzip', 'deflate') else None)
        length = response.headers.get('Content-Length')
        if length is not None:
            try:
                length = int(length)
            except ValueError:
                raise ResponseLimitError('Некорректный размер ответа') from None
            if length < 0 or length > max_bytes:
                raise ResponseLimitError('Ответ источника превышает лимит')
        body = bytearray()
        wire_size = 0
        for chunk in _chunks(response.raw):
            check_collection()
            wire_size += len(chunk)
            if wire_size > max_bytes:
                raise ResponseLimitError('Ответ источника превышает лимит')
            try:
                decoded = decoder.decompress(chunk, max_bytes-len(body)+1) if decoder else chunk
            except zlib.error:
                raise ResponseLimitError('Повреждённый сжатый ответ') from None
            body.extend(decoded)
            if len(body) > max_bytes:
                raise ResponseLimitError('Ответ источника превышает лимит')
            if decoder and decoder.unused_data:
                raise ResponseLimitError('Лишние данные после сжатого ответа')
            check_collection()
        if length is not None and wire_size != length:
            raise ResponseLimitError('Неполный ответ источника')
        if decoder and not decoder.eof:
            raise ResponseLimitError('Неполный сжатый ответ')
        check_collection()
        response._content = bytes(body)
        response._content_consumed = True
        return response
    except UrllibHTTPError:
        raise requests.RequestException('Ошибка чтения ответа источника') from None
    finally:
        response.close()


class SourceSession(requests.Session):
    def get_redirect_target(self, response):
        # Session.send prepares Response.next even with allow_redirects=False;
        # the default redirect generator consumes response.content first.
        return None


def _request(method, url, **kwargs):
    with SourceSession() as session:
        return session.request(method, url, **kwargs)


def http_get(url: str, *, timeout: int | None = None, headers: dict | None = None,
             allow_redirects: bool = False, max_bytes: int = 12_000_000) -> requests.Response:
    global _last_request_at
    _pace()
    configured_timeout, user_agent = _http_settings.get()
    timeout = min(configured_timeout if timeout is None else timeout,check_collection()/2)
    h = {"User-Agent": user_agent, "Accept-Language": "ru,en;q=0.8",
         "Accept-Encoding": "gzip, deflate"}
    if headers:
        h.update(headers)
    try:
        response = _request("GET", url, timeout=timeout, headers=h,
                                allow_redirects=allow_redirects, stream=True)
        return _read_response(response, max_bytes)
    finally:
        _last_request_at = time.monotonic()


def http_post_json(url: str, payload: dict, *, timeout: int | None = None,
                   max_bytes: int = 12_000_000) -> requests.Response:
    """Public read APIs that use POST; shares bounded buffering with GET."""
    global _last_request_at
    _pace()
    configured_timeout, user_agent = _http_settings.get()
    timeout = min(configured_timeout if timeout is None else timeout,check_collection()/2)
    try:
        response = _request("POST", url, json=payload, timeout=timeout, stream=True,
                                 headers={"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"},
                                 allow_redirects=False)
        return _read_response(response, max_bytes)
    finally:
        _last_request_at = time.monotonic()
