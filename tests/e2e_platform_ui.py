"""Isolated UI verification. All writes intercepted; no website operations."""
import copy,json,os,sys,urllib.request
from pathlib import Path
from playwright.sync_api import sync_playwright,expect
base=os.environ.get('CLOAKUP_TEST_URL','http://127.0.0.1:8767')
fixture=json.load(urllib.request.urlopen(base+'/api/state'))
fixture['busy']=None
fixture.setdefault('definitions',[])
fixture.setdefault('readiness',None)
created=[];reports=[];runs=[]
out=Path('runs/verification/platform');out.mkdir(parents=True,exist_ok=True)
with sync_playwright() as p:
    browser=p.chromium.launch(executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless=True)
    ctx=browser.new_context(viewport={'width':1440,'height':1000},device_scale_factor=1)
    def route(r):
        url=r.request.url
        if url==base+'/api/state':r.fulfill(json=fixture);return
        if r.request.method=='POST':
            b=r.request.post_data_json
            if url==base+'/api/tasks' or '/api/tasks/' in url:
                old=next((t for t in fixture['definitions'] if t['id'] in url),None)
                item={'id':old['id'] if old else 'e'*32,'revision':old['revision']+1 if old else 1,'config':b['config']}
                if old:fixture['definitions'].remove(old)
                fixture['definitions'].insert(0,item);created.append(b);r.fulfill(json=item);return
            if url==base+'/api/preflight':
                reports.append(b);fixture['readiness']={'id':'f'*32,'status':'passed','started_at':1780000000,'fresh':True,'checks':[{'key':k,'title':t,'status':'passed','detail':'隔离回归测试','duration_ms':1} for k,t in [('database','数据库事务'),('storage','持久卷读写'),('model','模型网关响应'),('browser','浏览器与观察能力')]]};r.fulfill(json={'id':'f'*32});return
            if url==base+'/api/runs':
                runs.append(b);r.fulfill(json={'id':'a'*32});return
            raise AssertionError('Unexpected write '+url)
        if not url.startswith(base):r.abort();return
        r.continue_()
    ctx.route('**/*',route)
    page=ctx.new_page();page.set_default_timeout(5000);errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
    page.goto(base+'/?view=tasks',wait_until='networkidle')
    page.get_by_role('heading',name='自动化任务',exact=True).wait_for()
    page.get_by_role('button',name='新建任务',exact=True).first.click()
    page.get_by_label('任务名称',exact=True).fill('图书目录采集')
    page.get_by_label('网站地址',exact=True).fill('https://books.toscrape.com/')
    page.get_by_label('业务场景',exact=True).fill('公开目录采集')
    page.get_by_label('最终目标',exact=True).fill('查看第一本书，返回标题和来源链接')
    page.get_by_label('任务参数',exact=False).fill('{"limit": 1}')
    # Verify polling preserves drafts.
    page.wait_for_timeout(2700)
    assert page.get_by_label('最终目标',exact=True).input_value().startswith('查看第一本书')
    page.get_by_role('button',name='保存任务',exact=True).first.click()
    page.get_by_text('任务配置已保存为 v1',exact=True).wait_for()
    assert created[-1]['config']['params']=={'limit':1}
    expect(page.locator('#task-revision')).to_have_value('1')
    page.locator('[data-nav=tasks]').click()
    page.get_by_role('heading',name='自动化任务',exact=True).wait_for()
    if '--screenshots' in sys.argv:page.screenshot(path=str(out/'tasks-desktop.png'),full_page=True)
    page.get_by_role('button',name='环境检查',exact=True).click()
    page.get_by_role('button',name='快速测试',exact=True).click()
    page.get_by_text('环境已通过，可继续执行任务',exact=True).wait_for()
    assert len(reports)==1 and page.locator('.readiness-row').count()==4
    if '--screenshots' in sys.argv:page.screenshot(path=str(out/'readiness-desktop.png'),full_page=True)
    # Task launch must use platform orchestration, never the legacy explorer.
    page.locator('[data-nav=tasks]').click()
    page.get_by_role('button',name='配置任务',exact=True).first.click()
    page.get_by_role('button',name='仅探索',exact=True).click()
    assert runs[-1]=={'mode':'automation_task','task_id':'e'*32,'params':{'explore_only':True}}
    for width in [1024,760,390,320]:
        page.set_viewport_size({'width':width,'height':900})
        for nav in ['tasks','readiness','execution','settings','sourcing','chat']:
            page.locator('[data-nav='+nav+']').first.click()
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'),(width,nav)
    page.locator('[data-nav=tasks]').first.click();page.get_by_role('button',name='配置任务',exact=True).first.click()
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    if '--screenshots' in sys.argv:page.screenshot(path=str(out/'task-mobile.png'),full_page=True)
    assert not errors,errors
    browser.close()
    print(json.dumps({'page_errors':errors,'config_persistence':'verified','platform_launch':'verified','readiness':'verified','responsive_widths':[1024,760,390,320]},ensure_ascii=False))
