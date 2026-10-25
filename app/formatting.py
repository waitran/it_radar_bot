"""Bounded, escaped Telegram HTML. Never truncate an HTML tag or URL."""
import html
import re
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo


def units(text):
    return len(text.encode('utf-16-le')) // 2


def escaped_clip(text, budget):
    parts, used = [], 0
    for char in str(text or ''):
        escaped = html.escape(char, quote=True)
        length = units(escaped)
        if used + length > budget:
            break
        parts.append(escaped)
        used += length
    return ''.join(parts)


def link(value, label):
    try:
        p = urlsplit(value or '')
        if p.scheme not in {'https','http'} or not p.hostname or p.username or p.password:
            return None
        if any(ord(c) < 32 or c in '<>"' for c in value):
            return None
        escaped = html.escape(value,quote=True)
        return f'<a href="{escaped}">{label}</a>' if units(escaped) <= 1200 else None
    except ValueError:
        return None


def original_url(item):
    """Telegram source identity points at a post, including legacy stored rows."""
    channel = re.fullmatch(r'tg_([A-Za-z0-9_]{1,64})', item.source_key)
    if channel:
        identifier = str(item.external_id)
        if re.fullmatch(r'[1-9][0-9]{0,18}', identifier):
            return f'https://t.me/{channel[1]}/{identifier}'
        # Never replace missing post evidence with a channel homepage.
        return None
    return item.url


def source_text(item, budget=180):
    """Avoid bare channel URLs that Telegram turns into competing hyperlinks."""
    channel = re.fullmatch(r'tg_([A-Za-z0-9_]{1,64})', item.source_key)
    if channel:
        name = re.sub(r'(?:https?://)?t\.me/(?:s/)?[A-Za-z0-9_]+',
                      channel[1], item.source_name, flags=re.I)
        label = escaped_clip(name, budget)
        return link(original_url(item), label) or label
    return escaped_clip(item.source_name, budget)


def format_message(item, rules):
    labels = [rules.categories[c]['label'] for c in item.categories if c in rules.categories]
    lines = ['📡 <b>Новая возможность</b>',
             'Категория: '+escaped_clip(', '.join(labels) or '—',250),
             'Источник: '+source_text(item),
             '<b>'+escaped_clip(item.title,650)+'</b>']
    if item.summary:
        lines.append(escaped_clip(item.summary,1100))
    status = 'набор/регистрация открыты (по тексту объявления)' if item.registration_status=='open' else 'статус регистрации неизвестен — проверьте на странице'
    lines.append('Статус: '+status)
    if item.deadline:
        tz=ZoneInfo(rules.timezone)
        date=item.deadline.astimezone(tz) if item.deadline.tzinfo else item.deadline.replace(tzinfo=tz)
        lines.append(f'Срок: {date:%Y-%m-%d %H:%M} ({rules.timezone})')
    for value,label in [(original_url(item),'Оригинал'),(item.apply_url,'Заявка/регистрация')]:
        rendered=link(value,label)
        if rendered and units('\n'.join([*lines,rendered]))<=3900:
            lines.append(rendered)
    return '\n'.join(lines)
