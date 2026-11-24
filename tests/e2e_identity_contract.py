"""Identity evidence: no real site, no network, no candidate data."""
import json
from playwright.sync_api import sync_playwright
from cloakup.boss_chat import IDENTITY_READER_JS,SNAPSHOT_JS,MESSAGE_STATUS_JS
with sync_playwright() as p:
 browser=p.chromium.launch(executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless=True);page=browser.new_page()
 def fixture(name='甲顾问',job='岗位',extra=''):
  page.set_content('''<div id="container"><aside><div class="geek-item" data-id="12-0"><span class="geek-name">甲顾问</span><span class="source-job">岗位</span></div></aside><section><div>沟通详情</div><header><div>其他说明</div><b>'''+name+'''</b><span>在线简历</span><div><span>沟通职位：</span><a>'''+job+'''</a></div></header><div class="message-item"><span>甲顾问</span></div></section>'''+extra+'</div>')
 def read(title='岗位',cid='12-0',name='甲顾问'):
  return page.evaluate('()=>{const r=('+IDENTITY_READER_JS+')('+json.dumps(title)+','+json.dumps(cid)+','+json.dumps(name)+');delete r.headerElement;return r}')
 fixture();assert read()['valid'] # Name is deliberately not the first line.
 fixture(extra='<div class="geek-item" data-id="12-0" style="display:none"><span class="geek-name">甲顾问</span><span class="source-job">岗位</span></div>');assert read()['valid']
 fixture(name='<span>甲</span><span>顾问</span>');assert read()['valid']
 fixture(name='<span>甲</span><span>顾问二</span>');assert not read()['valid']
 # 认证徽标与拆分姓名位于非固定类名容器：整元素渲染文字相等即可，仍拒绝隐藏文字与不同姓名。
 page.set_content('''<div id="container"><aside><div class="geek-item selected" data-id="12-0"><span class="geek-name">甲顾问</span><span class="source-job">岗位</span></div></aside><section><header><div class="name-box"><span class="base-name"><span>甲</span><span>顾问</span><i class="verify-icon"></i></span><span>刚刚活跃</span></div><span>在线简历</span><div><span>沟通职位：</span><a>岗位</a></div></header><div class="message-item"><span>你好</span></div></section></div>''');assert read()['valid']
 page.set_content('''<div id="container"><aside><div class="geek-item selected" data-id="12-0"><span class="geek-name">甲顾问</span><span class="source-job">岗位</span></div></aside><section><header><div class="name-box"><span class="base-name">甲\u200b顾问</span></div><span>在线简历</span><div><span>沟通职位：</span><a>岗位</a></div></header><div class="message-item"><span>你好</span></div></section></div>''');assert read()['valid']
 page.set_content('''<div id="container"><aside><div class="geek-item selected" data-id="12-0"><span class="geek-name">甲顾问</span><span class="source-job">岗位</span></div></aside><section><header><div class="name-box"><span class="base-name"><span>甲</span><span style="display:none">顾问</span></span></div><span>在线简历</span><div><span>沟通职位：</span><a>岗位</a></div></header><div class="message-item"><span>你好</span></div></section></div>''');assert not read()['valid']
 page.set_content('''<div id="container"><aside><div class="geek-item selected" data-id="12-0"><span class="geek-name">甲顾问</span><span class="source-job">岗位</span></div></aside><section><header><div class="name-box"><span class="base-name"><span>甲</span><span>顾问二</span><i class="verify-icon"></i></span></div><span>在线简历</span><div><span>沟通职位：</span><a>岗位</a></div></header><div class="message-item"><span>你好</span></div></section></div>''');assert not read()['valid']
 fixture(name='乙顾问');assert not read()['valid'] # A message mentioning the target name is not identity evidence.
 fixture(job='岗位二');assert not read()['valid'] # Substring job matching is forbidden.
 fixture();assert not read(cid='13-0')['valid']
 fixture(extra='<div class="geek-item" data-id="13-0"><span class="geek-name">甲顾问</span><span class="source-job">岗位</span></div>');assert not read()['valid']
 # Platform's observed .selected marker disambiguates identical display names.
 fixture(extra='<div class="geek-item" data-id="13-0"><span class="geek-name">甲顾问</span><span class="source-job">岗位</span></div>')
 page.locator('[data-id="12-0"]').evaluate("e=>e.classList.add('selected')");assert read()['valid']
 assert not read(cid='13-0')['valid']
 page.locator('[data-id="13-0"]').evaluate("e=>e.classList.add('selected')");assert not read()['valid']
 fixture();snap=SNAPSHOT_JS.replace('IDENTITY_READER',IDENTITY_READER_JS).replace('STATUS_FUNCTION',MESSAGE_STATUS_JS).replace('CHAT_ID',json.dumps('12-0')).replace('EXPECTED_NAME',json.dumps('甲顾问')).replace('EXPECTED_TITLE',json.dumps('岗位'));assert page.evaluate(snap)['valid']
 browser.close();print('identity contract: layout prefix, hidden duplicates, wrong recipient, wrong job, wrong ID, ambiguous names and shared snapshot passed')
