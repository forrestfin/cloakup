"""Saved task plan is visible and editable; no mutation requests permitted."""
import json,urllib.request
from playwright.sync_api import sync_playwright,expect
base='http://127.0.0.1:8766';s=json.load(urllib.request.urlopen(base+'/api/state'));t=next(t for t in s['definitions'] if t['config']['engine']=='boss_sourcing')
with sync_playwright() as p:
 b=p.chromium.launch(executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless=True);page=b.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
 def route(r):
  assert r.request.method!='POST','read-only plan display must not write'
  r.continue_()
 page.route('**/*',route);page.goto(base+'/?view=tasks',wait_until='networkidle');page.locator('[data-task-workspace="'+t['id']+'"]').first.click()
 expect(page.get_by_role('heading',name='探索与实施参考计划',exact=True)).to_be_visible()
 expect(page.get_by_text('来源：LLM 生成',exact=False)).to_be_visible()
 page.get_by_role('button',name='编辑目标与参数',exact=True).click();page.locator('summary').filter(has_text='探索计划与版本').click()
 value=page.locator('#task-plan').input_value();assert json.loads(value)['steps']
 expect(page.get_by_text('来源：LLM 生成',exact=False)).to_be_visible()
 assert not errors,errors;b.close();print('task-owned LLM plan visible, source shown, JSON preloaded: passed')
