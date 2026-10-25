"""Bounded HTML pages backed by indexed SQLite data; no source HTTP."""
from datetime import datetime
from zoneinfo import ZoneInfo
from app.formatting import escaped_clip, link, units, original_url, source_text
from app.keyboards import button, pagination, settings


def render(db, uid, kind, page=0):
    prefs=db.preferences(uid)
    if kind=='settings':
        return ('Персональные настройки\nГород: '+escaped_clip(prefs['city'] or 'любой',150)+'\nЧасовой пояс: '+escaped_clip(prefs['timezone'],100), settings(uid,prefs))
    tz=ZoneInfo(prefs['timezone'])
    if kind=='reminders':
        rows=db.reminders_page(uid,page); lines=['Напоминания']; keyboard=[]
        for i,row in enumerate(rows[:3],1):
            when=datetime.fromtimestamp(row['due_at'],tz)
            lines.append(f"{i}. {escaped_clip(row['title'],300)}\n{when:%d.%m.%Y %H:%M} ({escaped_clip(prefs['timezone'],100)}), за {row['days']} дн.")
            keyboard.append([button(f'Отменить {i}',f'cancel:{uid}:{row["id"]}')])
    else:
        rows=db.personal_page(uid,kind,page); lines=['⭐ Избранное' if kind=='saved' else '📅 Ближайшие семь дней']; keyboard=[]
        for i,row in enumerate(rows[:3],1):
            lines.append(f"\n{i}. <b>{escaped_clip(row['title'],300)}</b>")
            if row['event_at'] is not None:
                value=datetime.fromtimestamp(row['event_at'],tz)
                lines.append('Дата: '+(row['event_day'] if row['event_precision']=='date' else value.strftime('%d.%m.%Y %H:%M')+' '+escaped_clip(prefs['timezone'],100)))
            else:lines.append('Дата проведения неизвестна')
            labels={'online':'онлайн','offline':'офлайн','hybrid':'онлайн и офлайн','unknown':'неизвестен'}
            lines.append('Формат: '+labels[row['format']]+(' · '+escaped_clip(row['city'],100) if row['city'] else ''))
            item=db.deserialize(row['payload']) if row['payload'] else None
            lines.append('Источник: '+(source_text(item,120) if item else escaped_clip(row['source_key'],120)))
            if row['deadline_at'] is not None:
                value=datetime.fromtimestamp(row['deadline_at'],tz)
                lines.append('Дедлайн: '+(row['deadline_day'] if row['deadline_precision']=='date' else value.strftime('%d.%m.%Y %H:%M')+' '+escaped_clip(prefs['timezone'],100)))
            url=original_url(item) if item else row['url']
            original=link(url,'Оригинал')
            if original:
                if units(original)<=600:lines.append(original)
                else:keyboard.append([{'text':f'Оригинал {i}','url':url}])
            ref=row['ref']
            if kind=='saved':
                keyboard.append([button(f'Удалить {i}',f'unsave:{uid}:{ref}'),button('За 3 дня',f'remind:{uid}:{ref}:3'),button('За 1 день',f'remind:{uid}:{ref}:1')])
            else:keyboard.append([button(f'⭐ Сохранить {i}',f'save:{ref}')])
    if not rows:lines.append('Здесь пока пусто.')
    keyboard+=pagination(uid,kind,page,len(rows)>3)
    return '\n'.join(lines),{'inline_keyboard':keyboard}
