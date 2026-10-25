"""Validate private-chat sender before every action. No network while DB is locked."""
import logging
import re
from zoneinfo import ZoneInfoNotFoundError
from app.agenda import render
from app.preferences import CATEGORIES, TOPICS

log=logging.getLogger(__name__)


def private_sender(message, sender):
    if not isinstance(message,dict) or not isinstance(sender,dict):return False
    uid=sender.get('id');chat=message.get('chat',{})
    if not isinstance(chat,dict):return False
    return (type(uid) is int and uid>0 and not sender.get('is_bot',False)
            and chat.get('type')=='private' and chat.get('id')==uid)


def handle_callback(bot, update):
    query=update.get('callback_query')
    if not isinstance(query,dict):return
    sender=query.get('from') or {};message=query.get('message') or {};qid=query.get('id')
    if not isinstance(sender,dict):sender={}
    if not isinstance(message,dict):message={}
    if not isinstance(qid,str):return
    # Stop Telegram's spinner before state work; no bot/SQLite lock held.
    try:bot.notifier.call('answerCallbackQuery',{'callback_query_id':qid})
    except Exception:log.warning('answerCallbackQuery failed',exc_info=True)
    with bot._lock:
        offset=update.get('update_id')
        if type(offset) is not int or offset<int(bot.db.get_flag('offset','0')):return
        reply='Кнопка недействительна.';markup=None
        uid=sender.get('id')
        data=query.get('data')
        if private_sender(message,sender) and isinstance(data,str) and len(data.encode())<=64:
            try:
                fields=data.split(':');action=fields[0]
                if action=='save' and len(fields)==2 and re.fullmatch(r'[1-9][0-9]{0,17}',fields[1]):
                    reply='Сохранено. /saved — избранное.' if bot.db.save_item(uid,int(fields[1])) else 'Событие недоступно или не подходит настройкам.'
                elif len(fields)>=3 and fields[1]==str(uid):
                    if action=='page' and len(fields)==4 and fields[2] in ('saved','week','reminders') and fields[3].isdigit():
                        reply,markup=render(bot.db,uid,fields[2],int(fields[3]))
                    elif action=='pref' and len(fields) in (4,5):
                        prefs=bot.db.preferences(uid);group,key=fields[2:4]
                        labels={'categories':CATEGORIES,'topics':TOPICS}
                        if len(fields)==5 and fields[4] not in ('0','1'):raise ValueError('Invalid value')
                        if group in labels and (key in labels[group] or group=='topics' and key=='all'):
                            if key=='all':prefs[group]=[]
                            elif len(fields)!=5:raise ValueError('Missing state')
                            elif fields[4]=='0' and key in prefs[group]:prefs[group].remove(key)
                            elif fields[4]=='1' and key not in prefs[group]:prefs[group].append(key)
                        elif group=='format' and key in ('any','online','offline'):prefs[group]=key
                        else:raise ValueError('Invalid preference')
                        bot.db.set_preferences(uid,prefs);reply,markup=render(bot.db,uid,'settings')
                    elif action=='input' and len(fields)==3 and fields[2] in ('city','timezone'):
                        field=fields[2];bot.db.set_flag(f'preference_input:{uid}',field)
                        reply='Отправьте '+('город (или «любой»).' if field=='city' else 'часовой пояс IANA, например Europe/Moscow или Asia/Yekaterinburg.')
                    elif action=='unsave' and len(fields)==3 and fields[2].isdigit():
                        bot.db.remove_saved(uid,int(fields[2]));reply,markup=render(bot.db,uid,'saved')
                    elif action=='remind' and len(fields)==4 and fields[2].isdigit() and fields[3] in ('1','3'):
                        reply='Напоминание включено. /reminders' if bot.db.enable_reminder(uid,int(fields[2]),int(fields[3])) else 'Напоминание не создано: проверьте дедлайн, избранное и подписку. Уже отправленные задания и задания с неизвестным результатом не повторяются.'
                    elif action=='cancel' and len(fields)==3 and fields[2].isdigit():
                        bot.db.cancel_reminder(uid,int(fields[2]));reply,markup=render(bot.db,uid,'reminders')
            except (ValueError,TypeError,OverflowError,ZoneInfoNotFoundError):
                reply='Кнопка недействительна.'
        bot.db.user_control(uid if private_sender(message,sender) else 0,offset=offset+1,
                            reply=reply if private_sender(message,sender) else None,reply_markup=markup)


def consume_input(bot, uid, text):
    field=bot.db.get_flag(f'preference_input:{uid}','')
    if not field:return None
    prefs=bot.db.preferences(uid)
    if not text or len(text)>100:return 'Значение должно содержать от 1 до 100 символов.',None
    prefs[field]='' if field=='city' and text.casefold()=='любой' else text.strip()
    try:bot.db.set_preferences(uid,prefs)
    except (ValueError,TypeError,OverflowError,ZoneInfoNotFoundError):return 'Неизвестный часовой пояс. Используйте имя IANA, например Europe/Moscow.',None
    bot.db.set_flag(f'preference_input:{uid}','')
    return render(bot.db,uid,'settings')
