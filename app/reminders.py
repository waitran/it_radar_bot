"""Calendar arithmetic for date-only deadlines; absolute arithmetic otherwise."""
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


def due_at(metadata, days, timezone_name):
    if metadata['deadline_at'] is None: return None
    if metadata['deadline_precision'] == 'date':
        day = datetime.fromisoformat(metadata['deadline_day']).date()-timedelta(days=days)
        return datetime.combine(day, time(9), ZoneInfo(timezone_name)).timestamp()
    return metadata['deadline_at']-days*86400


def format_reminder(item, job, preferences):
    from app.formatting import escaped_clip, link, original_url, source_text
    tz = ZoneInfo(preferences['timezone'])
    deadline = (job['deadline_day'] if job['deadline_precision']=='date' else
                datetime.fromtimestamp(job['deadline_at'],tz).strftime('%d.%m.%Y %H:%M')+' '+preferences['timezone'])
    lines = ['⏰ <b>Дедлайн регистрации</b>',escaped_clip(item.title,650),
             'Срок: '+escaped_clip(deadline,150),'Источник: '+source_text(item),
             'Актуальность регистрации не подтверждена — проверьте оригинал.']
    original = link(original_url(item),'Оригинал')
    if original:lines.append(original)
    return '\n'.join(lines)
