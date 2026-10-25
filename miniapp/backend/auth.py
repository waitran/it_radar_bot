"""Telegram bot-token HMAC validation; signed user is the only identity source."""
import hashlib
import hmac
import json
import re
import time
from urllib.parse import parse_qsl
from fastapi import Header, HTTPException, Request
from pydantic import ValidationError
from .schemas import User


def validate_init_data(raw, settings, *, now=None):
    try:
        if not raw or len(raw) > 16384 or re.search(r'%(?![0-9a-fA-F]{2})', raw):
            raise ValueError
        pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True,
                          encoding='utf-8', errors='strict', max_num_fields=32)
        fields = dict(pairs)
        if len(fields) != len(pairs) or any(not re.fullmatch(r'[a-z_][a-z0-9_]{0,63}', key) for key in fields):
            raise ValueError
        received = fields.pop('hash')
        if not re.fullmatch('[0-9a-fA-F]{64}', received):
            raise ValueError
        check = '\n'.join(f'{key}={value}' for key, value in sorted(fields.items()))
        secret = hmac.digest(b'WebAppData', settings.bot_token.encode(), 'sha256')
        expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, received.lower()):
            raise ValueError
        if not re.fullmatch(r'[0-9]{1,12}', fields['auth_date']):
            raise ValueError
        timestamp = int(fields['auth_date'])
        age = (time.time() if now is None else now) - timestamp
        if age < -30 or age > settings.auth_ttl:
            raise ValueError
        return User.model_validate(json.loads(fields['user']))
    except (ValueError, KeyError, TypeError, UnicodeError, ValidationError):
        raise HTTPException(401, 'Invalid or expired Telegram authorization',
                            headers={'WWW-Authenticate': 'tma'}) from None


def current_user(request: Request, authorization: str | None = Header(default=None)):
    if not authorization or not authorization.startswith('tma '):
        raise HTTPException(401, 'Telegram authorization required', headers={'WWW-Authenticate': 'tma'})
    user = validate_init_data(authorization[4:], request.app.state.settings)
    if not request.app.state.user_limiter.allow(user.id):
        raise HTTPException(429, 'Request limit exceeded', headers={'Retry-After': '60'})
    return user
