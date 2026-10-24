"""Offline regression tests for bounded source downloads (A08)."""
import gzip
import threading
import unittest
import zlib
from unittest.mock import Mock, patch

import requests
from urllib3.exceptions import ProtocolError

from app.collectors.base import (http_get, http_post_json, collection_scope,
                                 CollectionCancelled, CollectionTimeout, ResponseLimitError)


class StreamingTests(unittest.TestCase):
    def reply(self, chunks, headers=None, status=200):
        response = requests.Response()
        response.status_code = status
        response.headers.update(headers or {})
        response.raw = Mock(stream=Mock(return_value=iter(chunks)))
        response.close = Mock()
        return response

    def download(self, response, limit=10, post=False):
        method = 'post' if post else 'get'
        with patch('app.collectors.base._pace'), patch('app.collectors.base._request', return_value=response) as send:
            result = http_post_json('https://example.test', {}, max_bytes=limit) if post else http_get('https://example.test', max_bytes=limit)
        self.assertTrue(send.call_args.kwargs['stream'])
        response.close.assert_called_once()
        return result

    def test_get_and_post_preserve_text_at_exact_limit(self):
        for post in (False, True):
            with self.subTest(post=post):
                response = self.reply([b'hello', b'world'], {'Content-Length': '10'})
                self.assertEqual(self.download(response, post=post).text, 'helloworld')

    def test_unknown_length_stops_before_remaining_chunks(self):
        seen = []
        def chunks():
            for chunk in (b'123456', b'78901', b'never'):
                seen.append(chunk)
                yield chunk
        response = self.reply(chunks())
        with self.assertRaises(ResponseLimitError): self.download(response)
        self.assertEqual(len(seen), 2)
        response.close.assert_called_once()
        self.assertFalse(response._content_consumed)

    def test_bad_or_excessive_length_rejected_before_body_read(self):
        for length in ('11', '-1', 'invalid'):
            response = self.reply([], {'Content-Length': length})
            with self.assertRaises(ResponseLimitError): self.download(response)
            response.raw.stream.assert_not_called()
            response.close.assert_called_once()

    def test_false_length_and_truncation_are_rejected(self):
        for data in (b'a', b'abcdef'):
            with self.assertRaises(ResponseLimitError):
                self.download(self.reply([data], {'Content-Length': '5'}))

    def test_compressed_expansion_is_bounded(self):
        for encoding, compressor in [('gzip', gzip.compress), ('deflate', zlib.compress)]:
            response = self.reply([compressor(b'x'*100_000)], {'Content-Encoding': encoding})
            with self.assertRaises(ResponseLimitError): self.download(response, limit=1000)
            response.close.assert_called_once()

    def test_valid_compressed_body_and_truncated_body(self):
        for encoding, compressor in [('gzip', gzip.compress), ('deflate', zlib.compress)]:
            data = compressor(b'hello')
            self.assertEqual(self.download(self.reply([data[:3], data[3:]], {'Content-Encoding': encoding}),100).text,'hello')
            with self.assertRaises(ResponseLimitError):
                self.download(self.reply([data[:-1]], {'Content-Encoding': encoding}),100)

    def test_unsupported_and_corrupt_compression(self):
        for encoding in ('br', 'gzip'):
            with self.assertRaises(ResponseLimitError):
                self.download(self.reply([b'bad'], {'Content-Encoding': encoding}))

    def test_http_error_closes_without_loading_body(self):
        response = self.reply([], status=403)
        with self.assertRaises(requests.HTTPError): self.download(response)
        response.raw.stream.assert_not_called()
        response.close.assert_called_once()

    def test_transport_failure_closes_and_uses_requests_exception(self):
        response = self.reply([])
        response.raw.stream.side_effect = ProtocolError('truncated')
        with self.assertRaises(requests.RequestException): self.download(response)
        response.close.assert_called_once()

    def test_cancel_between_chunks_closes(self):
        stop = threading.Event()
        def chunks():
            yield b'first'
            stop.set()
            yield b'last'
        response = self.reply(chunks())
        with self.assertRaises(CollectionCancelled), collection_scope(stop):
            self.download(response)
            # scope's exit check must not mask whether download raised
        stop.clear()
        response.close.assert_called_once()

    def test_deadline_between_chunks_closes(self):
        now = [0]
        def chunks():
            yield b'first'
            now[0] = 11
            yield b'last'
        response = self.reply(chunks())
        with patch('app.collectors.base.time.monotonic', side_effect=lambda:now[0]):
            with self.assertRaises(CollectionTimeout), collection_scope(budget=10):
                self.download(response)
        response.close.assert_called_once()

    def test_read1_path_checks_cancel_without_waiting_for_full_chunk(self):
        stop = threading.Event()
        class Raw:
            def read1(self, size, decode_content):
                stop.set()
                return b'partial'
        response = self.reply([])
        response.raw = Raw()
        with self.assertRaises(CollectionCancelled), collection_scope(stop):
            self.download(response)
        stop.clear()
        response.close.assert_called_once()

    def test_size_failure_is_a_safe_website_error(self):
        from app.collectors.website_http import read_text, WebsiteSourceError
        response = self.reply([], {'Content-Length': '12000001'})
        with patch('app.collectors.base._pace'), patch('app.collectors.base._request', return_value=response):
            with self.assertRaises(WebsiteSourceError): read_text('https://example.test')
        response.close.assert_called_once()

    def test_size_failure_is_a_safe_telegram_snapshot_error(self):
        from app.collectors.telegram_channel import fetch_channel, TelegramSourceError
        response = self.reply([], {'Content-Length': '8000001'})
        with patch('app.collectors.base._pace'), patch('app.collectors.base._request', return_value=response):
            with self.assertRaises(TelegramSourceError): fetch_channel('example')
        response.close.assert_called_once()

    def test_real_urllib3_reader_preserves_utf8_after_decompression(self):
        from io import BytesIO
        from urllib3.response import HTTPResponse
        data = 'Привет'.encode('utf-8')
        response = self.reply([], {'Content-Encoding': 'gzip'})
        response.encoding = 'utf-8'
        response.raw = HTTPResponse(body=BytesIO(gzip.compress(data)), preload_content=False)
        self.assertEqual(self.download(response,100).text, 'Привет')


class RedirectTransportTests(unittest.TestCase):
    def test_real_session_never_reads_redirect_bodies_or_follows_location(self):
        import io
        from app.collectors.base import http_post_json
        for status in (301,302,303,307,308):
            for method in ('GET','POST'):
                raw = io.BytesIO(b'x'*1_048_576)
                raw.read = Mock(wraps=raw.read)
                reply = requests.Response()
                reply.status_code = status
                reply.headers.update({'Location':'https://example.test/next','Content-Length':'1048576'})
                reply.raw = raw
                class Adapter(requests.adapters.BaseAdapter):
                    def send(self, request, **kwargs):
                        reply.request = request
                        reply.url = request.url
                        return reply
                    def close(self): pass
                adapter = Adapter()
                with self.subTest(status=status, method=method), patch('app.collectors.base._pace'), patch.object(adapter,'send',wraps=adapter.send) as send, patch('requests.sessions.Session.get_adapter',return_value=adapter):
                    result = (http_get('https://example.test',max_bytes=16) if method=='GET'
                              else http_post_json('https://example.test',{},max_bytes=16))
                self.assertEqual(result.status_code,status)
                self.assertIsNone(result.next)
                send.assert_called_once()
                raw.read.assert_not_called()
                self.assertTrue(raw.closed)
