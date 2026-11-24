"""Task prompt editor persists the field; real configuration writes intercepted."""
import json,urllib.request
from playwright.sync_api import sync_playwright,expect
base='http://127.0.0.1:8766';s=json.load(urllib.request.urlopen(base+'/api/state'));t=next(t for t in s['definitions'] if t['config']['engine']=='boss_sourcing');writes=[]
with sync_playwright() as p:
 b=p.chromium.launch(executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless=True);page=b.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
 def route(r):
  if r.request.url==base+'/api/state':r.fulfill(json=s)
  elif r.request.method=='POST':
   assert r.request.url.endswith('/api/tasks/'+t['id']);writes.append(r.request.post_data_json)
   t.update(config=writes[-1]['config'],revision=t['revision']+1);r.fulfill(json=t)
  else:r.continue_()
 page.route('**/*',route);page.goto(base+'/?view=tasks',wait_until='networkidle');page.locator('[data-task-workspace="'+t['id']+'"]').first.click();page.get_by_role('button',name='编辑目标与参数',exact=True).click()
 page.locator('summary').filter(has_text='规划提示词（可编辑）').click()
 expect(page.locator('#task-planning-prompt')).not_to_have_value('')
 page.locator('#task-planning-prompt').fill('先核对已确认的远程安排，再规划资料获取。')
 page.get_by_role('button',name='保存任务',exact=True).first.click();page.wait_for_timeout(300)
 assert writes[-1]['config']['planning_prompt']=='先核对已确认的远程安排，再规划资料获取。'
 page.locator('.sidebar [data-nav=settings]').click();expect(page.get_by_role('heading',name='LLM 运行时',exact=True)).to_be_visible()
 expect(page.get_by_text('DeepSeek Harness · 0.2.2rc2',exact=False)).to_be_visible()
 assert not errors,errors;b.close();print('editable task prompt save + Harness runtime settings: passed (writes intercepted)')
