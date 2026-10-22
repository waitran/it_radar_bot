"""Redact credentials at the output boundary, including formatted tracebacks."""
from __future__ import annotations

import logging
import os
import re
from urllib.parse import quote, quote_plus


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        # Also hide malformed/configured credentials, not only recognized tokens.
        secrets = set()
        for name, value in os.environ.items():
            if value and any(part in name.upper() for part in ("TOKEN", "SECRET", "PASSWORD", "API_KEY")):
                for secret in (value, value.strip()):
                    if secret:
                        secrets.update((secret, quote(secret, safe=""), quote_plus(secret)))
        for secret in sorted(secrets, key=len, reverse=True):
            text = text.replace(secret, "[REDACTED]")
        # Recognize Telegram tokens even when they are not the configured token.
        text = re.sub(r"[0-9]{5,}(?::|%3[aA])[A-Za-z0-9_-]{20,}", "[REDACTED]", text)
        text = re.sub(r"\b(?:github_pat_|gh[pousr]_)[A-Za-z0-9_]+", "[REDACTED]", text)
        return text


def configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(RedactingFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    # The standalone application owns its output handlers, including library logs.
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
