"""Offline probes for the 2026-10-25 audit; no production data or external HTTP.

Run from the repository root: python -m scripts.audit_2026_10_25
The JSON records observed defects; it is not a passing regression test suite.
All databases are temporary and Telegram responses are mocked.
"""
import sys
from pathlib import Path
sys.path.insert(0,str(Path.cwd()))
import io, json, os, tempfile, threading, time, copy, subprocess
from unittest.mock import Mock, patch
from datetime import datetime, timezone, timedelta
import requests, yaml
from app.collectors.base import http_get
from app.config import Config, TelegramChannelSource
from app.main import Bot, handle_update
from app.models import Item
from app.notify import DeliveryResult
from app.filters import Filters
from app.formatting import format_message

ROOT=Path.cwd()
results={}
class Raw(io.BytesIO):
    consumed=0
    def read(self,*args,**kwargs):
        value=super().read(*args)
        self.consumed+=len(value)
        return value
class Adapter(requests.adapters.BaseAdapter):
    def send(self,request,**kwargs):
        response=requests.Response();response.status_code=302
        response.headers.update({'Location':'/next','Content-Length':str(1024*1024)})
        response.url=request.url;response.request=request
        self.raw=Raw(b'x'*(1024*1024));response.raw=self.raw
        return response
    def close(self): pass
adapter=Adapter()
with patch('app.collectors.base._pace'),patch('requests.sessions.Session.get_adapter',return_value=adapter):
    response=http_get('https://example.test/redirect',max_bytes=16)
results['redirect_limit_bypass']={'limit':16,'body_read':adapter.raw.consumed,'status':response.status_code,'requests_version':requests.__version__}

with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{},clear=True):
    cfg=Config.load(str(ROOT/'config.yaml'));cfg.db_path=str(Path(directory)/'state.db');cfg.owner_id=0
    cfg.channels=[TelegramChannelSource('example','example')];cfg.websites=[]
    bot=Bot(cfg);bot.notifier.send=Mock(return_value=DeliveryResult('sent',message_id=1))
    entered=threading.Event();release=threading.Event()
    def blocked(*args,**kwargs):
        entered.set();release.wait(5);return []
    with patch('app.main.telegram_channel.fetch_channel',side_effect=blocked):
        worker=threading.Thread(target=bot.run_cycle);worker.start()
        assert entered.wait(3)
        handle_update(bot,{'update_id':1,'message':{'from':{'id':101},'chat':{'id':101,'type':'private'},'text':'/start'}})
        results['commands_wait_for_scan']={'active':bool(bot.db.user(101)['is_active']),'pending_reply':bot.db.pending_reply(time.time()) is not None,'send_calls_while_blocked':bot.notifier.send.call_count}
        release.set();worker.join(5);assert not worker.is_alive()
        results['commands_wait_for_scan']['send_calls_after_scan']=bot.notifier.send.call_count
    bot.db.close()

with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{},clear=True):
    cfg=Config.load(str(ROOT/'config.yaml'));cfg.db_path=str(Path(directory)/'state.db');cfg.owner_id=0
    cfg.channels=[TelegramChannelSource('example','example')];cfg.websites=[]
    bot=Bot(cfg);bot.notifier.send=Mock(return_value=DeliveryResult('sent',message_id=1))
    now=datetime.now(timezone.utc)
    bot.db.user_control(101,active=True)
    entry=Item('tg_example','Example','1','https://t.me/example/1','Стажировка Python','Открыт набор для студентов без опыта',None,now+timedelta(seconds=1),raw_text='Открыт набор для студентов без опыта')
    bot.db.add_item(entry,category='internships');bot._available_sources={'tg_example'};bot._last_scan=now.timestamp();bot._fresh_items={entry.item_key:entry}
    closed = copy.deepcopy(entry)
    closed.raw_text = 'Приём заявок закрыт. Стажировка Python для студентов без опыта'
    with patch('app.main.telegram_channel.fetch_post', return_value=closed) as refresh:
        sent=bot.drain_pending(now=now+timedelta(minutes=30))
    results['cache_revalidation']={'age_minutes':30,'refresh_calls':refresh.call_count,'sent':sent}
    bot.db.close()

with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{},clear=True):
    raw=yaml.safe_load((ROOT/'config.yaml').read_text())
    raw['rules']['categories']['internships']['label']=['invalid']
    path=Path(directory)/'config.yaml';path.write_text(yaml.safe_dump(raw,allow_unicode=True))
    cfg=Config.load(str(path));cfg.bot_token='123456789:'+'synthetic_value_'*2;cfg.validate_runtime()
    entry=Item('tg_example','Example','1','https://t.me/example/1','Стажировка Python','Открыт набор для студентов без опыта',None,datetime.now(timezone.utc),raw_text='Открыт набор для студентов без опыта')
    checked = subprocess.run([sys.executable, '-m', 'app.main', '--check-config'],
        cwd=ROOT, env={'TELEGRAM_BOT_TOKEN':cfg.bot_token, 'CONFIG_PATH':str(path)},
        capture_output=True, text=True, timeout=10)
    accepted=Filters(cfg.rules).check(entry)[0]
    try:format_message(entry,cfg.rules); error=None
    except Exception as exc:error=type(exc).__name__
    results['invalid_label_accepted']={'config_passes':True,'check_config_exit_code':checked.returncode,'item_accepted':accepted,'format_error':error}
# A markup change can preserve post parsing while hiding the pagination link.
from app.collectors.telegram_channel import fetch_channel, TelegramSourceError
html = ''.join(
    f'<div class="tgme_widget_message_wrap"><div class="tgme_widget_message" data-post="example/{identifier}">'
    '<a class="tgme_widget_message_date"><time datetime="2026-10-25T10:00:00Z"></time></a>'
    '<div class="tgme_widget_message_text">Стажировка Python</div></div></div>'
    for identifier in (100,101))
html += '<a class="changed-pagination" data-before="100" href="/s/example?before=100">Older</a>'
response = requests.Response();response.status_code=200;response._content=html.encode();response.encoding='utf-8'
with patch('app.collectors.telegram_channel.http_get',return_value=response) as get:
    try:
        entries = fetch_channel('example',since_id=1)
        history_error = None
    except TelegramSourceError as exc:
        entries = []
        history_error = str(exc)
results['incomplete_history_accepted'] = {'saved_boundary':1,'accepted_ids':[int(entry.external_id) for entry in entries],
    'http_calls':get.call_count,'older_link_present_with_changed_class':True,'error':history_error}
with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{},clear=True):
    cfg=Config.load(str(ROOT/'config.yaml'));cfg.db_path=str(Path(directory)/'state.db');cfg.owner_id=0
    cfg.channels=[TelegramChannelSource('example','example')];cfg.websites=[]
    bot=Bot(cfg);bot.db.save_telegram_cursor('tg_example',1)
    with patch('app.collectors.telegram_channel.http_get',return_value=response):
        bot.run_cycle()
    results['incomplete_history_accepted']['persisted_cursor_after_cycle'] = bot.db.telegram_cursor('tg_example')
    bot.db.close()

print(json.dumps(results,ensure_ascii=False,indent=2))
