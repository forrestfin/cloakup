"""Local UI regression using isolated Chrome and mocked write APIs.
Run with a bound job in the running workbench:
  .venv/bin/python tests/e2e_enterprise_ui.py
Add --screenshots to capture the three design review views.
No real config, recruiting task, message or platform account is mutated.
"""
import json,urllib.request,copy,sys,os
from pathlib import Path
from playwright.sync_api import sync_playwright,expect
base=os.environ.get('CLOAKUP_TEST_URL','http://127.0.0.1:8765')
fixture=json.load(urllib.request.urlopen(base+'/api/state'))
out=Path('runs/verification/enterprise-ui');out.mkdir(parents=True,exist_ok=True)
submitted=[]
with sync_playwright() as p:
 browser=p.chromium.launch(executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless=True)
 ctx=browser.new_context(viewport={'width':1440,'height':1000},device_scale_factor=1)
 def route(r):
  url=r.request.url
  if url==base+'/api/state':r.fulfill(json=fixture);return
  if '/api/cases/' in url and url.endswith('/config') and r.request.method=='POST':
   b=r.request.post_data_json;submitted.append(b);c=next(c for c in fixture['cases'] if c['id'] in url)
   c['config_revision']=b['expected_revision']+1;c['editable_config']={**b['config'],'revision':c['config_revision']};c['job']['snapshot']['requirements']=b['config']['jd'];r.fulfill(json=c['editable_config']);return
  if r.request.method=='POST':raise AssertionError('Unexpected mutation '+url)
  if not url.startswith(base):r.abort();return
  r.continue_()
 ctx.route('**/*',route)
 page=ctx.new_page();page.set_default_timeout(5000);errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
 page.goto(base+'/?view=sourcing',wait_until='networkidle')
 page.get_by_role('heading',name='岗位管理',exact=True).wait_for()
 assert page.locator('.topbar').bounding_box()['height']==56
 assert not page.get_by_text('图书练习站点').count()
 if '--screenshots' in sys.argv:page.screenshot(path=str(out/'jobs-desktop.png'),full_page=True)
 # Preserve edit base and all fields across independent settings tabs.
 page.get_by_role('button',name='配置岗位',exact=True).first.click()
 page.get_by_label('执行使用的 JD',exact=True).fill('界面回归：已修改的岗位要求')
 page.get_by_role('tab',name='执行策略',exact=True).click()
 page.get_by_label('检查间隔（秒）',exact=True).fill('180')
 if '--screenshots' in sys.argv:page.screenshot(path=str(out/'job-settings-desktop.png'),full_page=True)
 page.get_by_role('tab',name='沟通配置',exact=True).click()
 page.get_by_role('button',name='保存配置',exact=True).click()
 page.get_by_text('配置已保存，未发送的旧草稿已失效',exact=True).wait_for()
 assert len(submitted)==1 and submitted[0]['config']['jd']=='界面回归：已修改的岗位要求'
 assert submitted[0]['config']['execution']['poll_seconds']==180
 expect(page.locator('[name=expected_revision]')).to_have_value(str(fixture['cases'][0]['config_revision']))
 page.locator('[data-nav=sourcing]').click()
 page.get_by_role('checkbox',name='选择全部岗位',exact=True).check()
 page.get_by_role('button',name='创建自动化任务',exact=True).click()
 page.get_by_role('heading',name='新建自动化任务',exact=True).wait_for()
 assert page.get_by_label('场景适配器',exact=True).input_value()=='boss_sourcing'
 assert page.locator('[name=case_ids]:checked').count()==len(fixture['cases'])
 # Navigation does not start any tasks.
 page.get_by_role('button',name='候选人沟通',exact=True).click()
 page.get_by_role('heading',name='候选人沟通',exact=True).wait_for()
 if page.get_by_role('tab',name='执行依据').count():
  page.get_by_role('tab',name='执行依据').click()
  page.get_by_role('tab',name='候选资料').click()
  page.get_by_role('tab',name='沟通记录').click()
 for view,title in [('audit','审计记录'),('settings','系统设置')]:
  page.locator('[data-nav='+view+']').first.click();page.get_by_role('heading',name=title,exact=True).wait_for()
 for width in [1024,760,390,320]:
  page.set_viewport_size({'width':width,'height':900})
  for nav in ['sourcing','chat','settings','execution']:
   page.locator('[data-nav='+nav+']').first.click()
   assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'),(width,nav)
 page.set_viewport_size({'width':390,'height':900});page.locator('[data-nav=chat]').first.click()
 if '--screenshots' in sys.argv:page.screenshot(path=str(out/'conversations-mobile.png'),full_page=True)
 assert not errors,errors
 browser.close()
 print(json.dumps({'page_errors':errors,'config_save_payload':'verified','tabs_and_selection':'verified','responsive_widths':[1024,760,390,320],'screenshots':3 if '--screenshots' in sys.argv else 0},ensure_ascii=False))
