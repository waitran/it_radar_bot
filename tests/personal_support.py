from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock
from app.config import Config, TelegramChannelSource
from app.main import Bot, handle_update
from app.models import Item
from app.notify import DeliveryResult

ROOT=Path(__file__).resolve().parents[1]

class PersonalCase(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.cfg=Config.load(str(ROOT/'config.yaml'));self.cfg.db_path=str(Path(self.tmp.name)/'state.db');self.cfg.owner_id=0
        self.cfg.channels=[TelegramChannelSource('example','example')];self.cfg.websites=[]
        self.bot=Bot(self.cfg);self.addCleanup(lambda:self.bot.db.close())
        self.bot.notifier.send=Mock(return_value=DeliveryResult('sent',message_id=42))
        self.bot.notifier.call=Mock(return_value=True)
        self.db=self.bot.db;self.counter=0
        for uid in (101,202):self.db.user_control(uid,active=True)
        self.db._conn.execute('UPDATE users SET subscribed_at=?',(time.time()-86400,));self.db._conn.commit()
        self.bot._available_sources={'tg_example'};self.bot._last_scan=time.time()

    def event(self, identifier='1', text=None):
        now=datetime.now(timezone.utc)
        day=(now+timedelta(days=6)).strftime('%Y-%m-%d')
        start=(now+timedelta(days=2)).strftime('%Y-%m-%d')
        item=Item('tg_example','Example',identifier,f'https://t.me/example/{identifier}',
                  'Митап Python для начинающих '+identifier,'',None,now,
                  raw_text=text or f'Митап состоится {start} в 18:00.\nРегистрация до {day}.\nОнлайн. Город: Москва.',categories=['events'])
        self.db.add_item(item,category='events')
        self.bot._fresh_items[item.item_key]=item
        return item,self.db.item_ref(item.item_key)

    def callback(self, data, uid=101, chat=None):
        self.counter+=1
        handle_update(self.bot,{'update_id':self.counter,'callback_query':{'id':str(self.counter),'from':{'id':uid},
            'message':{'chat':{'id':uid if chat is None else chat,'type':'private'}},'data':data}})

    def command(self, text, uid=101):
        self.counter+=1
        handle_update(self.bot,{'update_id':self.counter,'message':{'chat':{'id':uid,'type':'private'},'from':{'id':uid},'text':text}})
