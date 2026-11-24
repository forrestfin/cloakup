"""Selection recovery UI uses synthetic state and intercepts every write."""
import copy
import json
import threading
import urllib.request
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


state = json.load(urllib.request.urlopen('http://127.0.0.1:8766/api/state'))
task = copy.deepcopy(next(t for t in state['definitions'] if t['config']['engine'] == 'boss_sourcing'))
case = copy.deepcopy(next(c for c in state['cases'] if c['id'] in task['config']['case_ids']))
case.update(id='a' * 32, candidates={}, audit=[])
case['job']['label'] = '恢复验收岗位'
task['id'] = 'b' * 32
task['config'].update(name='恢复验收任务', case_ids=[case['id']])
other = copy.deepcopy(task)
other['id'] = 'c' * 32
other['config'].update(name='未受影响的任务', case_ids=[])
owner = {'id': 'd' * 32, 'mode': 'boss_autopilot', 'account_runner': True,
         'task': '账号执行器', 'status': 'running', 'created': 1794877200,
         'params': {'case_ids': [case['id']]}, 'events': [], 'autopilot_stage': 'selection_retry'}
recovery = {'task_id': task['id'], 'status': 'retrying', 'attempts': 1, 'max_attempts': 3,
            'reason': '会话列表发生变化，正在重新核验选中状态', 'next_check_at': 4102444800,
            'updated_at': 1794877500, 'diagnostics': {'stage': 'select_chat', 'reason': 'covered', 'exception_type': 'TimeoutError'},
            'failures': ['DO_NOT_SHOW_INTERNAL_CHAT_ID']}
owner['selection_recovery'] = {case['id']: recovery}
state.update(definitions=[task, other], cases=[case], runs=[owner], busy=owner['id'],
             attention=[], task_plans=[], planning_dialogues=[], planning_requests=[])
state['account_execution'] = {'label': 'BOSS · 测试账号', 'owner_run_id': owner['id'],
                              'browser_url': 'http://127.0.0.1:6080/vnc.html?test=bound', 'browser_alive': True,
                              'members': [{'id': task['id'], 'status': 'running'}, {'id': other['id'], 'status': 'paused'}]}
state['runtime_status'].update(task_id=owner['id'], task_state='会话选择自动恢复中', needs_attention=False,
                               browser_alive=True, selection_recovery=[{**recovery, 'case_id': case['id']}])
writes, errors = [], []


class Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


server = ThreadingHTTPServer(('127.0.0.1', 0), partial(Quiet, directory=str(Path(__file__).resolve().parents[1] / 'cloakup/web')))
threading.Thread(target=server.serve_forever, daemon=True).start()
base = 'http://127.0.0.1:' + str(server.server_port)
try:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', headless=True)
        page = browser.new_page(viewport={'width': 1440, 'height': 1000})
        page.on('pageerror', lambda e: errors.append(str(e)))

        def route(r):
            if r.request.url == base + '/api/state':
                r.fulfill(json=state)
            elif r.request.method == 'POST':
                writes.append((r.request.url, r.request.post_data_json))
                r.fulfill(json={'ok': True})
            elif r.request.url.startswith(base + '/'):
                r.continue_()
            else:
                r.abort()

        def open_task(ident):
            page.goto(base + '/?view=tasks', wait_until='networkidle')
            page.locator('[data-task-workspace="' + ident + '"]').first.click()

        def update_recovery(status, attempts):
            recovery.update(status=status, attempts=attempts, updated_at=recovery['updated_at'] + 60)
            state['runtime_status']['selection_recovery'] = [{**recovery, 'case_id': case['id']}]

        page.route('**/*', route)
        open_task(task['id'])
        expect(page.locator('[data-selection-recovery]')).to_have_count(0)
        assert 'DO_NOT_SHOW_INTERNAL_CHAT_ID' not in page.locator('body').inner_text()
        open_task(other['id'])
        expect(page.locator('[data-selection-recovery]')).to_have_count(0)
        expect(page.get_by_role('button', name='继续', exact=True)).to_be_visible()

        # Blocked selection is different from an administrator's ordinary pause.
        update_recovery('blocked', 3)
        owner.update(status='paused', autopilot_stage='selection_blocked')
        state['account_execution']['members'][0]['status'] = 'paused'
        state['runtime_status'].update(task_state='任务受阻 · 等待管理员', needs_attention=True)
        state['attention'] = [{'id': 'e' * 32, 'kind': 'execution_recovery', 'title': '会话选择需要管理员处理',
                               'detail': recovery['reason'], 'task_id': task['id'], 'run_id': owner['id'],
                               'status': 'pending', 'created_at': recovery['updated_at'], 'history': [],
                               'case_id': case['id'], 'recovery_attempts': 3, 'recovery_max_attempts': 3}]
        page.goto(base + '/?view=tasks', wait_until='networkidle')
        row = page.locator('tbody tr').filter(has_text='恢复验收任务')
        expect(row).to_contain_text('核验失败 · 等待管理员')
        expect(row.get_by_role('button', name='处理后继续', exact=True)).to_be_visible()
        open_task(task['id'])
        panel = page.locator('[data-selection-recovery]')
        expect(panel.get_by_role('heading', name='核验失败 · 等待管理员', exact=True)).to_be_visible()
        expect(panel).to_contain_text('累计恢复尝试 3 / 3')
        assert '预计核验：' not in panel.inner_text()
        panel.get_by_role('button', name='管理员待办', exact=True).click()
        expect(page.get_by_role('heading', name='会话选择需要管理员处理', exact=True)).to_be_visible()
        expect(page.get_by_role('button', name='打开任务并处理', exact=True)).to_be_visible()
        expect(page.get_by_role('button', name='更新岗位事实', exact=True)).to_have_count(0)
        expect(page.get_by_role('link', name='打开执行浏览器', exact=True)).to_have_attribute('href', state['account_execution']['browser_url'])

        page.goto(base + '/?view=execution&run=' + owner['id'], wait_until='networkidle')
        expect(page.locator('[data-selection-recovery]')).to_contain_text('累计恢复尝试 3 / 3')
        expect(page.locator('.run-summary')).to_contain_text('核验失败 · 等待管理员')
        page.locator('.sidebar [data-nav=environments]').click()
        expect(page.locator('[data-selection-recovery]')).to_contain_text(recovery['reason'])
        expect(page.locator('#connection')).to_have_text('服务已连接')

        # Resuming a blocked task keeps its incident but no longer claims it is paused.
        state['account_execution']['members'][0]['status']='running'
        open_task(task['id'])
        expect(page.locator('[data-selection-recovery]')).to_have_attribute('data-recovery-status','paused')
        expect(page.locator('[data-selection-recovery]')).to_contain_text('任务已暂停，不读取或操作网站')
        expect(page.get_by_role('button',name='暂停',exact=True)).to_be_visible()
        state['account_execution']['members'][0]['status']='paused'

        # Direct automation runs have a separate page renderer from the account executor.
        direct = {**owner, 'id': 'f' * 32, 'account_runner': False, 'mode': 'automation_task',
                  'task_id': task['id'], 'task_snapshot': task, 'phase': 'implementation'}
        state['runs'].append(direct)
        page.goto(base + '/?view=execution&run=' + direct['id'], wait_until='networkidle')
        expect(page.locator('[data-selection-recovery]')).to_contain_text('累计恢复尝试 3 / 3')
        expect(page.locator('.run-summary')).to_contain_text('核验失败 · 等待管理员')

        # No current browser means no generic browser creation action from this panel.
        state['account_execution']['browser_alive'] = False
        open_task(task['id'])
        expect(page.locator('[data-selection-recovery]').get_by_role('button', name='暂无已绑定浏览器')).to_be_disabled()
        expect(page.locator('[data-selection-recovery] a')).to_have_count(0)
        page.set_viewport_size({'width': 390, 'height': 900})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.set_viewport_size({'width': 1440, 'height': 1000})
        update_recovery('recovered', 3)
        owner.update(status='running', autopilot_stage='sync_history')
        state['account_execution']['members'][0]['status'] = 'running'
        state['runtime_status'].update(task_state='任务执行中', needs_attention=False)
        open_task(task['id'])
        expect(page.locator('[data-selection-recovery]').get_by_role('heading', name='会话已核验恢复', exact=True)).to_be_visible()
        assert '异常待处理' not in page.locator('body').inner_text()
        # Login requirements take precedence over a generic pending recovery.
        owner.update(status='waiting',active_case_id=case['id'],interaction={'id':'access-test','kind':'browser_access','reason':'login','prompt':'请完成登录'})
        state['account_execution']['members'][0]['status']='running'
        state['account_execution']['browser_alive']=True
        update_recovery('retrying',3)
        page.goto(base+'/?view=tasks',wait_until='networkidle')
        expect(page.locator('#task-blocker-alert')).to_contain_text('需要重新登录')
        expect(page.locator('tbody tr').filter(has_text='恢复验收任务')).to_contain_text('需要重新登录')
        open_task(task['id'])
        expect(page.locator('.access-panel')).to_contain_text('等待登录 BOSS')
        expect(page.locator('[data-check-access="access-test"]')).to_have_attribute('data-check-run',owner['id'])
        owner['interaction']=None;state['runtime_status']['selection_recovery']=[]
        page.evaluate('refresh(true)')
        expect(page.locator('#task-blocker-alert')).to_be_hidden()
        assert not errors, errors
        assert not writes, writes
        browser.close()
        print('selection retry/block/recovery, task scoping, bound browser and admin entry: passed; no real writes')
finally:
    server.shutdown()
    server.server_close()
