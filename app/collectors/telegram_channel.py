"""Read public t.me/s pages only. No accounts, MTProto or access workarounds."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

from app.collectors.base import http_get
from app.models import Item

log = logging.getLogger("telegram")
USERNAME = re.compile(r"[A-Za-z0-9_]{1,64}")
CTA = re.compile(r"регистр\w*|зарегистр\w*|подать\s+(?:заявк|резюме)|подач[аиу]\s+заяв|"
                 r"заполн\w*\s+(?:анкет|форм)|отклик\w*|\bapply\b|\bregister\b|sign\s*up", re.I)
MARKER = re.compile(r"TGAPPLICATIONLINK\d+END")


class TelegramSourceError(RuntimeError):
    """Incomplete, inaccessible or changed public source; do not advance state."""


def _text(node) -> str:
    copy = BeautifulSoup(str(node), "html.parser")
    for br in copy.select("br"):
        br.replace_with("\n")
    for block in copy.select("p, div"):
        block.append("\n")
    text = re.sub(r"[ \t\u00a0]+", " ", copy.get_text())
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _safe_url(value: str) -> str | None:
    if not value or re.search(r'[\s<>"\x00-\x1f]', value):
        return None
    try:
        value = urljoin("https://t.me/", value)
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            return None
        # Channel/post/social links are not registration forms. A Telegram bot's
        # explicit start link can be used when accompanied by an application CTA.
        host = parsed.hostname.lower()
        if host in {"t.me", "telegram.me"}:
            if not any(k == "start" for k, _ in parse_qsl(parsed.query)):
                return None
        if host in {"max.ru", "vk.com", "www.vk.com"}:
            return None
        return value
    except ValueError:
        return None


def _application_url(node) -> str | None:
    copy = BeautifulSoup(str(node), "html.parser")
    anchors = []
    for index, anchor in enumerate(copy.select("a[href]")):
        marker = f"TGAPPLICATIONLINK{index}END"
        label = anchor.get_text(" ", strip=True)
        anchors.append((marker, label, _safe_url(anchor["href"])))
        anchor.replace_with(f"{marker} {label}")
    lines = _text(copy).splitlines()
    candidates = []
    for marker, label, url in anchors:
        if not url:
            continue
        score = 0
        if CTA.search(label):
            score = 3
        for index, line in enumerate(lines):
            if marker not in line or len(MARKER.findall(line)) != 1:
                continue
            if CTA.search(line):
                score = max(score, 2)
            elif index and lines[index - 1].rstrip().endswith(":") and CTA.search(lines[index - 1]):
                score = max(score, 2)
        path = urlsplit(url).path.lower()
        if re.search(r"/(?:register|registration|apply)(?:/|$)", path):
            score = max(score, 1)
        if score:
            candidates.append((score, url))
    if not candidates:
        return None
    best = max(score for score, _ in candidates)
    urls = list(dict.fromkeys(url for score, url in candidates if score == best))
    # The model has one apply_url field. Do not arbitrarily select among forms.
    return urls[0] if len(urls) == 1 else None


def _parse_page(html: str, username: str) -> tuple[list[Item], int | None]:
    soup = BeautifulSoup(html, "html.parser")
    wraps = soup.select("div.tgme_widget_message_wrap")
    if not wraps:
        raise TelegramSourceError("Публичные публикации не найдены: канал недоступен или изменена разметка.")
    items = {}
    for wrap in wraps:
        msg = wrap.select_one("div.tgme_widget_message[data-post]")
        if msg is None:
            raise TelegramSourceError("Изменена разметка сообщения: отсутствует data-post.")
        match = re.fullmatch(r"([A-Za-z0-9_]+)/(\d+)", msg["data-post"])
        if not match or match[1].lower() != username.lower() or int(match[2]) < 1:
            raise TelegramSourceError("Неожиданный канал или ID публикации на публичной странице.")
        post_id = int(match[2])
        date = msg.select_one(".tgme_widget_message_date time[datetime]")
        if date is None:
            raise TelegramSourceError(f"У публикации {post_id} отсутствует дата в ожидаемой разметке.")
        try:
            published_at = datetime.fromisoformat(date["datetime"].replace("Z", "+00:00"))
            if published_at.tzinfo is None:
                raise ValueError("missing timezone")
            published_at = published_at.astimezone(timezone.utc)
        except ValueError:
            raise TelegramSourceError(f"У публикации {post_id} некорректная дата.") from None
        text_node = msg.select_one("div.tgme_widget_message_text")
        text = _text(text_node) if text_node else ""
        # Keep media-only IDs to advance the cursor without inventing content.
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        title = next((line for line in lines if re.search(r"[\w]", line)), "")[:200]
        items[post_id] = Item(
            source_key=f"tg_{username.lower()}", source_name=f"Telegram-канал t.me/{username}",
            external_id=str(post_id), url=f"https://t.me/{username}/{post_id}",
            title=title, summary=text[:400], raw_text=text,
            apply_url=_application_url(text_node) if text_node else None,
            published_at=published_at,
        )
    older = soup.select("a.tme_messages_more[data-before]")
    if soup.select("a.tme_messages_more") and not older:
        raise TelegramSourceError("Изменена разметка пагинации Telegram.")
    before = None
    for anchor in older:
        try:
            parsed = urlsplit(urljoin(f"https://t.me/s/{username}", anchor.get("href", "")))
            query = parse_qsl(parsed.query, keep_blank_values=True)
            if (parsed.scheme != "https" or parsed.netloc != "t.me" or parsed.fragment
                    or parsed.path.lower() != f"/s/{username.lower()}"
                    or len(query) != 1 or query[0][0] != "before" or not query[0][1].isdigit()
                    or query[0][1] != anchor["data-before"]):
                raise ValueError("invalid page link")
            candidate = int(query[0][1])
            if candidate != min(items) or before is not None and before != candidate:
                raise ValueError("invalid cursor")
            before = candidate
        except ValueError:
            raise TelegramSourceError("Неожиданная ссылка пагинации; переход не выполнялся.") from None
    return [items[key] for key in sorted(items)], before


def fetch_channel(username: str, *, since_id: int | None = None, max_pages: int = 25) -> list[Item]:
    """Initial snapshot: one page. Thereafter fetch all posts newer than since_id.

    Return only a complete result, oldest first. Any failure/limit raises without
    returning a partial batch. The caller persists its cursor after ingestion.
    """
    if not isinstance(username, str) or not USERNAME.fullmatch(username):
        raise TelegramSourceError("Некорректное имя публичного Telegram-канала.")
    if since_id is not None and (type(since_id) is not int or since_id < 1):
        raise TelegramSourceError("Некорректный сохранённый ID Telegram.")
    if type(max_pages) is not int or max_pages < 1:
        raise TelegramSourceError("max_pages должен быть положительным целым числом.")
    base = f"https://t.me/s/{username}"
    before = None
    results = {}
    previous_min = None
    for page in range(max_pages):
        url = base if before is None else f"{base}?before={before}"
        try:
            response = http_get(url, allow_redirects=False)
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else "unknown"
            raise TelegramSourceError(f"Telegram HTTP {status}; обход ограничений не выполняется.") from None
        except requests.RequestException as exc:
            raise TelegramSourceError(f"Ошибка сети Telegram: {type(exc).__name__}.") from None
        if response.status_code != 200:
            raise TelegramSourceError(f"Telegram HTTP {response.status_code}: перенаправление или недоступная страница.")
        if len(response.content) > 8_000_000:
            raise TelegramSourceError("Страница Telegram превышает допустимый размер.")
        items, next_before = _parse_page(response.text, username)
        oldest = min(int(item.external_id) for item in items)
        if previous_min is not None and oldest >= previous_min:
            raise TelegramSourceError("Telegram повторяет страницу истории; частичный результат отклонён.")
        previous_min = oldest
        for item in items:
            identifier = int(item.external_id)
            if since_id is None or identifier > since_id:
                results.setdefault(identifier, item)
        if since_id is None or oldest <= since_id or next_before is None:
            log.info("channel %s: %d posts, %d pages", username, len(results), page + 1)
            return [results[key] for key in sorted(results)]
        before = next_before
    raise TelegramSourceError("Достигнут max_pages до сохранённого ID. Увеличьте max_pages; граница не сдвинута.")
