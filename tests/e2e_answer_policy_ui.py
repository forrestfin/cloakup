"""Exercise real local preview only; candidate sends and config writes forbidden."""
import json,urllib.request
from playwright.sync_api import sync_playwright,expect
base='http://127.0.0.1:8766';s=json.load(urllib.request.urlopen(base+'/api/state'))
t=next(t for t in s['definitions'] if t['config']['engine']=='boss_sourcing')
with sync_playwright() as p:
 b=p.chromium.launch(executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless=True)
 page=b.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
 def route(r):
  if r.request.method=='POST':assert r.request.url.endswith('/answer-preview'),r.request.url
  r.continue_()
 page.route('**/*',route);page.goto(base+'/?view=tasks',wait_until='networkidle');page.locator('[data-task-workspace="'+t['id']+'"]').first.click()
 page.locator('[data-workspace-tab=strategy]').click();page.locator('[data-job-tab=answering]').click()
 expect(page.get_by_role('heading',name='回答策略',exact=True)).to_be_visible()
 page.locator('[id^=answer-preview-]').first.fill('请问工作地方在哪')
 page.get_by_role('button',name='查看试答与依据',exact=True).click()
 box=page.locator('[id^=answer-preview-result-]');expect(box).to_contain_text('直接回答');expect(box).to_contain_text('远程协作');expect(box).to_contain_text('已确认岗位事实')
 page.locator('[id^=answer-preview-]').first.fill('请问工作地点和薪资是多少？');page.get_by_role('button',name='查看试答与依据',exact=True).click()
 expect(box).to_contain_text('需要核实并生成待办')
 assert not errors,errors
 b.close();print('real local preview: known location answered / incomplete salary question deferred / no candidate writes')
