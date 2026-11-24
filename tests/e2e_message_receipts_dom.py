"""Local synthetic DOM: prove status belongs to exactly one message, not its text."""
from playwright.sync_api import sync_playwright
from cloakup.boss_chat import MESSAGE_STATUS_JS
with sync_playwright() as p:
 b=p.chromium.launch(executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless=True);page=b.new_page()
 def check(html,expected):
  page.set_content(html)
  got=page.evaluate('(fn=>{const e=document.querySelector(".message-item");return fn(e,e.querySelector(".text-content"))})('+MESSAGE_STATUS_JS+')')
  assert got['status']==expected,got
 check('<div class="message-item"><div class="text-content">您好</div><span>已读</span></div>','read')
 check('<div class="wrapper"><div class="message-item"><div class="text-content">您好</div></div><span>已读</span></div>','read')
 check('<div><div class="message-item"><div class="text-content">您好</div></div><div class="message-item"><div class="text-content">另一条</div><span>已读</span></div></div>','unverified')
 check('<div class="message-item"><div class="text-content">我已读过岗位要求</div></div>','unverified')
 check('<div class="message-item"><div class="text-content">您好</div><span>未读</span></div>','unverified')
 check('<div class="wrapper"><div class="message-item"><div class="text-content">您好</div></div><span>已送达</span></div>','delivered')
 check('<div class="wrapper"><div class="message-item"><div class="text-content">您好</div></div><span>正在发送</span></div>','unknown')
 b.close();print('PASS: inline/sibling read and delivered labels; adjacent message/body text/unread excluded; sending remains unknown')
