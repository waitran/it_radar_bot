"""Short callbacks use sender IDs plus server-side event IDs, never URLs."""
from app.preferences import CATEGORIES, TOPICS


def button(label, data):
    if len(data.encode())>64: raise ValueError('Callback exceeds Telegram limit')
    return {'text': label, 'callback_data': data}


def save_keyboard(ref):
    return {'inline_keyboard': [[button('⭐ Сохранить', f'save:{ref}')]]} if ref else None


def settings(uid, prefs):
    rows=[]
    for group, labels in [('categories', CATEGORIES), ('topics', TOPICS)]:
        for key,label in labels.items():
            rows.append([button(('✓ ' if key in prefs[group] else '○ ')+label,f'pref:{uid}:{group}:{key}:{int(key not in prefs[group])}')])
    rows.append([button('Все направления',f'pref:{uid}:topics:all')])
    rows.append([button(('✓ ' if prefs['format']==key else '')+label,f'pref:{uid}:format:{key}') for key,label in [('online','Онлайн'),('offline','Офлайн'),('any','Любой')]])
    rows.extend([[button('Город',f'input:{uid}:city'), button('Часовой пояс',f'input:{uid}:timezone')]])
    return {'inline_keyboard': rows}


def pagination(uid, kind, page, has_next):
    row=[]
    if page:row.append(button('←',f'page:{uid}:{kind}:{page-1}'))
    if has_next:row.append(button('→',f'page:{uid}:{kind}:{page+1}'))
    return [row] if row else []
