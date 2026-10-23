"""Bounded, escaped Telegram HTML. Never truncate an HTML tag or URL."""
import html
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


def format_message(item, rules):
    labels = [rules.categories[c]['label'] for c in item.categories if c in rules.categories]
    lines = ['📡 <b>Новая возможность</b>',
             'Категория: '+escaped_clip(', '.join(labels) or '—',250),
             'Источник: '+escaped_clip(item.source_name,180),
             '<b>'+escaped_clip(item.title,650)+'</b>']
    if item.summary:
        lines.append(escaped_clip(item.summary,1100))
    status = 'набор/регистрация открыты (по тексту объявления)' if item.registration_status=='open' else 'статус регистрации неизвестен — проверьте на странице'
    lines.append('Статус: '+status)
    if item.deadline:
        tz=ZoneInfo(rules.timezone)
        date=item.deadline.astimezone(tz) if item.deadline.tzinfo else item.deadline.replace(tzinfo=tz)
        lines.append(f'Срок: {date:%Y-%m-%d %H:%M} ({rules.timezone})')
    for value,label in [(item.url,'Оригинал'),(item.apply_url,'Заявка/регистрация')]:
        rendered=link(value,label)
        if rendered and units('\n'.join([*lines,rendered]))<=3900:
            lines.append(rendered)
    return '\n'.join(lines)
