"""Loopback-only integration: unique disposable volumes, synthetic credentials, NO bot polling."""
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
import requests

TEST_ENV = None
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.db import Database
from app.models import Item


def run(args, *, check=True):
    result = subprocess.run(args, cwd=ROOT, text=True, capture_output=True, env=TEST_ENV)
    if check and result.returncode:
        # Never dump environment, authorization or complete Compose config.
        raise RuntimeError('Container command failed: ' + ' '.join(args[:4]) + '\n' + result.stderr[-3000:])
    return result


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def main():
    global TEST_ENV
    name = 'miniapp-audit-' + secrets.token_hex(6)
    catalog_volume = name + '-catalog'
    writer_name = name + '-writer'
    token = '999999:' + secrets.token_urlsafe(32)
    port = free_port()
    TEST_ENV = {key:value for key,value in os.environ.items() if not key.startswith(('MINIAPP_', 'BOT_', 'COMPOSE_', 'VITE_')) and key != 'TELEGRAM_BOT_TOKEN'}
    TEST_ENV.update(TELEGRAM_BOT_TOKEN=token, MINIAPP_DOMAIN='miniapp.example.invalid', BOT_DATA_VOLUME=catalog_volume, MINIAPP_LOCAL_PORT=str(port), BOT_UID='10001', BOT_GID='10001')
    with tempfile.TemporaryDirectory(prefix='miniapp-audit-') as directory:
        directory = Path(directory)
        envfile = directory/'compose.env'
        envfile.write_text(f'TELEGRAM_BOT_TOKEN={token}\nMINIAPP_DOMAIN=miniapp.example.invalid\nBOT_DATA_VOLUME={catalog_volume}\nMINIAPP_LOCAL_PORT={port}\n')
        envfile.chmod(0o600)
        compose = ['docker','compose','--env-file',str(envfile),'-p',name,'-f','compose.miniapp.yml','-f','compose.miniapp.local.yml']
        fixture = Database(str(directory/'bot.db'))
        fixture.set_flag('offset','91234')
        fixture.user_control(42, active=True)
        fixture.user_control(99, active=True)
        now = datetime.now(timezone.utc)
        for i in range(3):
            item = Item('integration','Structured fixture',str(i),f'https://t.me/example/{i+1}',
                        f'Python meetup {i}', 'Приглашаем на практический IT-митап', 'https://example.org/apply',
                        now - timedelta(days=i) if i != 2 else None)
            item.categories = ['events','contests']
            item.source_deadline = now + timedelta(days=10) if i == 0 else None
            fixture.add_item(item, category='events')
        with fixture._conn:
            fixture._conn.execute("INSERT INTO user_deliveries(telegram_id,item_key,status) VALUES(42,'integration:0','uncertain')")
        fixture.close()
        writer = directory/'writer.py'
        writer.write_text('''import fcntl,signal,sqlite3,time
lock=open('/catalog/bot.db.lock','a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
c=sqlite3.connect('/catalog/bot.db');c.execute('PRAGMA journal_mode=WAL');c.execute('BEGIN IMMEDIATE')
c.execute("UPDATE flags SET value='uncommitted' WHERE name='offset'")
open('/catalog/writer.ready','w').close()
def stop(*_):
 c.rollback();c.close();raise SystemExit(0)
signal.signal(signal.SIGTERM,stop)
while True:time.sleep(.1)
''')
        writer.chmod(0o644)
        def signed(uid=42, age=0):
            fields={'auth_date':str(int(time.time())-age), 'user':json.dumps({'id':uid,'first_name':'Integration'})}
            secret=hmac.digest(b'WebAppData',token.encode(),'sha256')
            fields['hash']=hmac.new(secret,'\n'.join(f'{k}={v}' for k,v in sorted(fields.items())).encode(),hashlib.sha256).hexdigest()
            return 'tma '+urlencode(fields)
        def request(method, path, uid=42, expected=200, **kwargs):
            headers=kwargs.pop('headers', {'Authorization':signed(uid)})
            response=requests.request(method,f'http://127.0.0.1:{port}{path}',headers=headers,timeout=12,**kwargs)
            assert response.status_code == expected, f'{method} {path}: {response.status_code}, expected {expected}'
            return response
        def container(service):
            return run(compose+['ps','-q',service]).stdout.strip()
        def wait_ready():
            until=time.monotonic()+60
            while time.monotonic()<until:
                try:
                    if requests.get(f'http://127.0.0.1:{port}/api/health',timeout=2).status_code==200:return
                except requests.RequestException:pass
                time.sleep(.3)
            raise RuntimeError('Isolated stack did not become ready')
        def fingerprint():
            code="import sqlite3,hashlib;c=sqlite3.connect('file:/catalog/bot.db?mode=ro',uri=True);print(hashlib.sha256('\\n'.join(c.iterdump()).encode()).hexdigest())"
            return run(['docker','exec',writer_name,'python','-c',code]).stdout.strip()
        try:
            run(['docker','volume','create',catalog_volume])
            seed = "import sqlite3,os;s=sqlite3.connect('file:/seed/bot.db?mode=ro',uri=True);d=sqlite3.connect('/catalog/bot.db');s.backup(d);d.close();s.close();os.chown('/catalog',10001,10001);os.chmod('/catalog',0o700);os.chown('/catalog/bot.db',10001,10001);os.chmod('/catalog/bot.db',0o600)"
            run(['docker','run','--rm','--network','none','--user','0:0','-v',str(directory)+':/seed:ro','-v',catalog_volume+':/catalog','it-radar-miniapp-api:local','python','-c',seed])
            run(['docker','run','-d','--name',writer_name,'--network','none','--user','10001:10001','-v',str(writer)+':/writer.py:ro','-v',catalog_volume+':/catalog','it-radar-miniapp-api:local','python','/writer.py'])
            for _ in range(30):
                if run(['docker','exec',writer_name,'python','-c',"import os;raise SystemExit(not os.path.exists('/catalog/writer.ready'))"],check=False).returncode==0:break
                time.sleep(.1)
            else:raise RuntimeError('Isolated SQLite writer did not start')
            before=fingerprint()
            run(compose+['up','-d','--no-build','--wait','--wait-timeout','90'])
            wait_ready()
            api_id=container('api');web_id=container('web')
            api_info=json.loads(run(['docker','inspect',api_id]).stdout)[0]
            assert not any(api_info['NetworkSettings']['Ports'].values()), 'API unexpectedly published'
            assert api_info['HostConfig']['ReadonlyRootfs']
            assert api_info['Config']['User']=='10001:10001'
            assert not next(m for m in api_info['Mounts'] if m['Destination']=='/catalog')['RW']
            web_info=json.loads(run(['docker','inspect',web_id]).stdout)[0]
            assert not any(value.startswith('TELEGRAM_BOT_TOKEN=') for value in web_info['Config']['Env'])
            denial="import sqlite3; c=sqlite3.connect('file:/catalog/bot.db?mode=ro',uri=True)\ntry:c.execute('DELETE FROM seen_items')\nexcept sqlite3.OperationalError:print('readonly write denied')\nelse:raise SystemExit(1)"
            assert 'readonly write denied' in run(['docker','exec',api_id,'python','-c',denial]).stdout
            assert run(['docker','exec',api_id,'python','-c',"import os;assert not os.path.exists('/srv/app/db.py')"]).returncode==0
            request('GET','/api/events',headers={},expected=401)
            request('GET','/api/me',headers={'Authorization':signed(age=86401)},expected=401)
            request('GET','/api/me',headers={'Authorization':'tma forged'},expected=401)
            assert request('GET','/api/me').json()['id']==42
            assert request('GET','/api/events?q=Python&category=contests&page_size=1').json()['total']==3
            assert request('GET','/api/events?active_only=true').json()['total']==1
            assert request('GET','/api/events?page=2&page_size=2').json()['items'][0]['published_at'] is None
            request('GET','/api/events?page_size=100',expected=422)
            for key in ['integration:0','integration:1','integration:0']:
                request('PUT','/api/favorites/'+key)
            assert request('GET','/api/favorites').json()['total']==2
            assert request('GET','/api/favorites',uid=99).json()['total']==0
            request('DELETE','/api/favorites/integration:0?telegram_id=42',uid=99)
            assert request('GET','/api/favorites').json()['total']==2
            assert request('GET','/api/events/integration:0').json()['is_favorite']
            assert not request('GET','/api/events/integration:0',uid=99).json()['is_favorite']
            request('PUT','/api/favorites/integration:0',data=b'x'*17000,expected=413)
            page=request('GET','/favorites',headers={})
            assert 'IT Radar' in page.text
            assert 'frame-ancestors' in page.headers['Content-Security-Policy']
            assert "script-src 'self'" in page.headers['Content-Security-Policy']
            assert 'X-Frame-Options' not in page.headers
            assert page.headers['X-Content-Type-Options']=='nosniff'
            request('GET','/api/missing',expected=404)
            request('GET','/assets/missing.js',headers={},expected=404)
            run(compose+['restart','api','web']);wait_ready()
            assert request('GET','/api/favorites').json()['total']==2
            run(compose+['up','-d','--no-deps','--force-recreate','api']);wait_ready()
            assert request('GET','/api/favorites').json()['total']==2
            result=run(compose+['run','--rm','--no-deps','maintenance','backup'])
            snapshot=json.loads(result.stdout.strip().splitlines()[-1])['file']
            restore_args=['run','--rm','--no-deps','maintenance','restore','/miniapp-backups/'+snapshot,'--replace','--safety-dir','/miniapp-backups']
            assert run(compose+restore_args,check=False).returncode!=0, 'Restore allowed while API was running'
            request('DELETE','/api/favorites/integration:1')
            run(compose+['stop','api'])
            run(compose+restore_args)
            run(compose+['start','api']);wait_ready()
            assert request('GET','/api/favorites').json()['total']==2
            assert before==fingerprint(), 'Catalog, offset or delivery history changed'
            print(json.dumps({'result':'passed','checks':['real HTTP API','HMAC','private favorites','limits','CSP','SPA routing','read-only catalog mount','WAL writer + bot lock','restart','container recreation','online backup','offline restore','running-API restore guard','unchanged catalog/offset/delivery history']}))
        except Exception:
            diagnostics=run(compose+['logs','--no-color','--tail','25','web','api'],check=False)
            print(diagnostics.stdout.replace(token,'[redacted]')[-6000:])
            raise
        finally:
            run(compose+['down','-v','--remove-orphans'],check=False)
            run(['docker','rm','-f',writer_name],check=False)
            run(['docker','volume','rm',catalog_volume],check=False)


if __name__=='__main__':main()
