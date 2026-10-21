"""Чтение публичных Telegram-каналов ТОЛЬКО через открытые веб-страницы t.me/s/<username>.

Не используются Telethon/Pyrogram/MTProto и пользовательский аккаунт.
Если канал закрыт для публичного просмотра, страница t.me/s/ не содержит сообщений —
это фиксируется как ошибка источника (обход ограничений не выполняется).
"""
from __future__ import annotations

import html as html_mod
import logging
import re
from datetime import datetime, timezone
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from app.collectors.base import http_get
from app.models import Item

log = logging.getLogger("telegram")

POST_RE = re.compile(r'data-post="(?P<user>[^/]+)/(?P<id>\d+)"')


def _parse_time(tag_text: str) -> datetime | None:
    m = re.search(r"datetime=\"([^\"]+)\"", tag_text)
    if not m:
        return None
    try:
        dt = datetime.fromisoformat(m.group(1))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def _clean(text: str) -> str:
    text = re.sub(r"[ \t\u00a0]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def fetch_channel(username: str) -> list[Item]:
    """Возвращает публикации канала (последняя страница ~20 постов), старые — новые первыми."""
    url = f"https://t.me/s/{username}"
    resp = http_get(url)
    soup = BeautifulSoup(resp.text, "html.parser")
    wraps = soup.select("div.tgme_widget_message_wrap")
    items: list[Item] = []
    for w in wraps:
        msg = w.select_one("div.tgme_widget_message")
        if not msg or not msg.has_attr("data-post"):
            continue
        post_id = msg["data-post"].split("/")[-1]
        time_tag = w.select_one("time")
        published_at = None
        if time_tag and time_tag.has_attr("datetime"):
            try:
                published_at = datetime.fromisoformat(time_tag["datetime"])
                if published_at.tzinfo is None:
                    published_at = published_at.replace(tzinfo=timezone.utc)
                published_at = published_at.astimezone(timezone.utc)
            except ValueError:
                pass
        text_el = w.select_one("div.tgme_widget_message_text")
        if not text_el:
            # публикация без текста (видео/фото/голосование) — пропускаем: фильтровать нечего
            continue
        links = []
        for a in text_el.find_all("a", href=True):
            href = urljoin("https:", a["href"]) if a["href"].startswith("//") else a["href"]
            if href.startswith("http"):
                links.append(href)
        full_text = _clean(text_el.get_text("\n"))
        # первая строка — заголовок
        first_line = full_text.split("\n", 1)[0].strip().lstrip("#").strip()
        summary = _clean(full_text)[:400]
        apply_url = None
        for l in links:
            low = l.lower()
            if any(d in low for d in ("forms.gle", "docs.google.com/forms", "t.me/", "reg.", "registr", "apply", "join", "clck.ru", "timepad", "hackathon", "skladka", "event.", "/join")):
                apply_url = l
                break
        permalink = f"https://t.me/{username}/{post_id}"
        items.append(Item(
            source_key=f"tg_{username.lower()}",
            source_name=f"Telegram-канал t.me/{username}",
            external_id=post_id,
            url=permalink,
            title=html_mod.unescape(first_line)[:200],
            summary=summary,
            apply_url=apply_url,
            published_at=published_at,
            raw_text=full_text,
        ))
    if not items:
        raise RuntimeError(
            f"На странице {url} не найдено ни одной текстовой публикации. "
            "Возможно, канал закрыт для публичного просмотра или требует входа."
        )
    items.reverse()
    log.info("channel %s: %d posts", username, len(items))
    return items
