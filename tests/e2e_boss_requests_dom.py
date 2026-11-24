from playwright.sync_api import sync_playwright
from cloakup.boss_requests import INSPECT_JS
with sync_playwright() as p:
 b=p.chromium.launch(executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless=True)
 page=b.new_page()
 page.set_content('''<div class="toolbar"><button>求简历</button><button>换电话</button><button>换微信</button></div><div class="message-item sent"><div class="text-content">方便通过平台交换电话或微信吗？</div></div><div class="message-item sent" data-mid="request"><div class="phone-card">我想和您交换电话，您是否同意</div></div><div class="message-item received" data-mid="actual"><div class="phone-card">手机号：13812345678</div></div>''')
 o=page.evaluate(INSPECT_JS)
 assert set(o['buttons'])=={'ask_resume','exchange_phone','exchange_wechat'}
 assert len(o['cards'])==2 and o['cards'][0]['id']=='request' and o['cards'][1]['incoming']
 page.set_content('''<div role="dialog">确认交换微信<button>确定</button><button>取消</button></div>''')
 assert 'exchange_wechat' in page.evaluate(INSPECT_JS)['confirmations']
 page.set_content('''<div role="dialog">购买套餐交换微信<button>确定</button></div>''')
 assert not page.evaluate(INSPECT_JS)['confirmations']
 b.close();print('PASS: 3 native buttons; text excluded from cards; incoming value attribution; scoped confirmation; payment dialog blocked')
