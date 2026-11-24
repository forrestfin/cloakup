"""Authentication recovery UI. All mutations intercepted, no business actions."""
import copy,json,os,urllib.request
from playwright.sync_api import sync_playwright,expect
base=os.environ.get('CLOAKUP_TEST_URL','http://127.0.0.1:8766')
fixture=json.load(urllib.request.urlopen(base+'/api/state'))
task=next(t for t in fixture['definitions'] if t['config']['engine']=='boss_sourcing')
jid='a'*32
job={'id':jid,'mode':'automation_task','task_id':task['id'],'task_snapshot':task,'params':{},'created':1794701000,'status':'failed','phase':'exploration','phases':[{'phase':'readiness'},{'phase':'exploration'}],'events':[],'error':'SourcingError: BOSS 已显示登录表单','can_recover_login':True,'result':None,'interaction':None}
fixture['runs']=[job];fixture['busy']=None
writes=[]
with sync_playwright() as p:
    browser=p.chromium.launch(executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless=True)
    ctx=browser.new_context(viewport={'width':1440,'height':1000})
    def route(r):
        url=r.request.url
        if url==base+'/api/state':r.fulfill(json=fixture);return
        if r.request.method=='POST':
            b=r.request.post_data_json;writes.append(b)
            assert url==base+'/api/runs/'+jid+'/action'
            if b['action']=='recover_login':
                job.update(status='waiting',error=None,can_recover_login=False,interaction={'id':'test-auth','kind':'browser_access','reason':'login','prompt':'请在操作浏览器登录，核验后继续当前任务'})
                fixture['busy']=jid;r.fulfill(json={'ok':True});return
            assert b=={'action':'check_access','interaction_id':'test-auth'}
            r.fulfill(status=400,json={'error':'尚未核验到已登录的招聘者页面，请在操作浏览器完成登录或校验'});return
        assert url.startswith(base)
        r.continue_()
    ctx.route('**/*',route)
    page=ctx.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
    page.goto(base+'/?view=execution&run='+jid,wait_until='networkidle')
    expect(page.get_by_role('button',name='登录并恢复此任务',exact=True)).to_be_visible()
    expect(page.get_by_role('link',name='导出完整记录',exact=True)).to_be_visible()
    page.get_by_role('button',name='登录并恢复此任务',exact=True).click()
    expect(page.get_by_role('heading',name='等待登录 BOSS',exact=True)).to_be_visible()
    assert page.get_by_role('link',name='打开登录浏览器',exact=True).get_attribute('href').startswith('http://127.0.0.1:6080/')
    assert page.locator('#human-reply').count()==0
    page.get_by_role('button',name='已登录，检测并继续',exact=True).click()
    expect(page.locator('#toast')).to_contain_text('尚未核验')
    expect(page.get_by_role('heading',name='等待登录 BOSS',exact=True)).to_be_visible()
    # Polling must reflect automatic login detection without another click.
    job.update(status='running',interaction=None);job['phases'].append({'phase':'implementation'});job['phase']='implementation'
    page.wait_for_timeout(2700)
    expect(page.get_by_role('heading',name='等待登录 BOSS',exact=True)).to_have_count(0)
    assert len(writes)==2
    for width in [390,320]:
        page.set_viewport_size({'width':width,'height':900})
        job.update(status='waiting',phase='exploration',interaction={'id':'next-auth','kind':'browser_access','reason':'verification','prompt':'请完成校验'})
        page.reload(wait_until='networkidle')
        expect(page.get_by_role('heading',name='等待完成平台校验',exact=True)).to_be_visible()
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    assert not errors,errors
    browser.close()
print(json.dumps({'legacy_recovery':'verified','login_controls':'verified','cannot_bypass_login':'verified','automatic_resume_render':'verified','page_errors':errors},ensure_ascii=False))
