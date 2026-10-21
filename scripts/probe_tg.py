import sys, re
sys.path.insert(0, '/workspace')
from bs4 import BeautifulSoup
raw = open('/tmp/tg_CTDLru.html', encoding='utf-8').read()
soup = BeautifulSoup(raw, 'html.parser')
wraps = soup.select('div.tgme_widget_message_wrap')
print('wraps:', len(wraps))
for w in wraps[:3]:
    t = w.select_one('time')
    print('time tag:', repr(t)[:250] if t else None)
    msg = w.select_one('div.tgme_widget_message')
    print('data-post:', msg.get('data-post') if msg else None)
print('datetime attrs:', re.findall(r'datetime="([^"]+)"', raw)[:5])
