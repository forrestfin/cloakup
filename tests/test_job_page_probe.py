"""11-24 多岗位任务：只读采集职位管理页与推荐页岗位切换控件结构（Task 6 观察入口）。"""
from types import SimpleNamespace as NS

import pytest

from cloakup import job_page_probe as probe
from cloakup import page_health

JOB_URL='https://www.zhipin.com/web/chat/job/edit?encryptId=abc123DEF456ghi789~~'


class Frame:
    def __init__(self,url):self.url=url


class Page:
    def __init__(self):
        self.url=probe.CHAT_URL;self.main_frame=Frame(probe.CHAT_URL);self.frames=[self.main_frame];self.clicks=[]
        self.mouse=NS(click=self._mouse)
    async def _mouse(self,x,y):self.clicks.append((x,y))


class Session:
    def __init__(self,page):self.page=page;self.navigations=[];self.acts=[]
    async def navigate(self,url):
        self.navigations.append(url);self.page.url=url
        if url==probe.RECOMMEND_URL:
            self.page.frames=[self.page.main_frame,Frame('https://www.zhipin.com/web/frame/recommend/?jobid=abc123DEF456ghi789~~&status=0')]
        elif url==probe.CHAT_URL:self.page.frames=[self.page.main_frame]
        else:self.page.frames=[self.page.main_frame,Frame('https://www.zhipin.com/web/frame/job/list?page=1')]
    async def _act(self,kind,fn):self.acts.append(kind);await fn()


def manager(status='paused',schedule=None,session=None,owner=None,root='.'):
    case={'id':'c1','job':{'label':'岗位甲','url':JOB_URL}}
    return NS(root=root,definitions=NS(get=lambda _:{'config':{'engine':'boss_sourcing','case_ids':['c1'],'schedule':schedule or {'enabled':False}}}),
              account_execution=NS(members=lambda:[{'status':status}]),busy=lambda:owner or {'account_runner':True},
              boss_session=session or Session(Page()),browser_alive=lambda _:True,cases=NS(load=lambda _:case))


@pytest.fixture
def fake_reads(monkeypatch):
    calls={'frame_clicks':[],'check':True,'visible':[['a'],['a','div.opts','div.opts > li']],'menu':[{'href':'/web/chat/job/list','path':'#menu > a','x':10,'y':20,'hit':True}],'switch':[{'path':'div.job-select','tag':'div','classes':'job-select','pointer':True}]}
    async def read_main(session,expression):
        if probe.JOB_MENU_TEXT in expression and 'leaves' in expression:return calls['menu']
        if expression.startswith('((document.body'):return 999
        if 'maxNodes' not in expression:raise AssertionError('unexpected main read')
        return {'html':'<div class="job-row" data-id="abc123DEF456ghi789~~">岗位甲</div><a href="/x?id=zz99887766aa">x</a>','nodes':3,'skipped':0,'truncated':False,'url':'/'}
    calls['frame_text']=[300]
    async def read_frame(session,frame,expression):
        if expression==probe.TEXT_LEN:calls['frame_text_reads']=calls.get('frame_text_reads',0)+1;return calls['frame_text'].pop(0) if len(calls['frame_text'])>1 else calls['frame_text'][0]
        if 'leaves' in expression:return calls['switch']
        if 'querySelector(css)' in expression:return calls['check']
        if 'out.length>=6000' in expression:return calls['visible'].pop(0) if calls['visible'] else []
        if 'maxNodes' not in expression:raise AssertionError('unexpected frame read')
        return {'html':'<ul class="job-options"><li data-jobid="abc123DEF456ghi789~~">岗位甲</li></ul>','nodes':2,'skipped':1,'truncated':False,'url':'/'}
    async def frame_click(session,frame,css):calls['frame_clicks'].append(css)
    async def check(session,kind):return {'healthy':True,'reason':None,'has_draft':False}
    async def read_frames(session,kind):return session.page.url,[{'login':False,'challenge':False}]
    async def no_sleep(_):return None
    monkeypatch.setattr(probe,'_read_main',read_main);monkeypatch.setattr(probe,'_read_frame',read_frame)
    monkeypatch.setattr(probe,'_frame_click',frame_click);monkeypatch.setattr(page_health,'check',check)
    monkeypatch.setattr(page_health,'read_frames',read_frames);monkeypatch.setattr(probe.asyncio,'sleep',no_sleep)
    def write(manager,capture):calls['capture']=capture;return 'analysis/job-pages-test.json'
    monkeypatch.setattr(probe,'_write_capture',write)
    return calls


def test_tokens_only_touch_links_and_data_attributes():
    tokens=probe.Tokens({'abc123DEF456ghi789~~':'岗位甲'})
    html=tokens.html('<li class="ui-dropdown-item12 job-card_wrap__3xK9a" ka="job_list_item_1" data-a="abc123DEF456ghi789~~" data-b="12345678901">数据分析师3年经验Python</li><a href="/web/x?encryptId=abc123DEF456ghi789~~">高级Java开发工程师2025届</a>')
    assert 'abc123DEF456ghi789' not in html and '12345678901' not in html and html.count('ID1')==2
    assert 'ui-dropdown-item12 job-card_wrap__3xK9a' in html and 'job_list_item_1' in html
    assert '数据分析师3年经验Python' in html and '高级Java开发工程师2025届' in html
    assert tokens.labels=={'ID1':'岗位甲'}


async def test_reads_both_pages_then_returns_to_chat_without_choosing_an_option(fake_reads):
    m=manager()
    m.boss_session._boss_recommend_list={'stale':True}
    report=await probe.read_job_pages(m,'t')
    s=m.boss_session
    assert s.navigations==['https://www.zhipin.com/web/chat/job/list',probe.RECOMMEND_URL,probe.CHAT_URL]
    # 只展开一次岗位控件，不点任何选项；回沟通页时自然收起
    assert fake_reads['frame_clicks']==['div.job-select']
    assert report['completed'] and report['returned_to_chat'] and report['business_writes']==0
    assert s._boss_recommend_list is None
    capture=fake_reads['capture']
    dumped=str(capture)+str(report)
    assert 'abc123DEF456ghi789' not in dumped and 'zz99887766aa' not in dumped
    assert report['known_ids']=={'ID1':'岗位甲'} and report['capture_file']=='analysis/job-pages-test.json'
    # 大块结构只进文件，执行器检查记录（会进 /api/state）只留摘要
    assert 'html' not in str(report) and 'html' in str(capture)
    expanded=capture['recommend']['expanded']
    assert expanded['performed'] and expanded['new_visible']==2 and 'new_parts' in expanded
    assert capture['recommend']['frame_url_before']==capture['recommend']['frame_url_after']


async def test_switch_control_not_unique_reads_without_clicking(fake_reads):
    fake_reads['switch']=[{'path':'a'},{'path':'b'}]
    await probe.read_job_pages(manager(),'t')
    assert fake_reads['frame_clicks']==[] and fake_reads['capture']['recommend']['expanded']=={'performed':False,'reason':'switch_control_not_unique'}


async def test_switch_control_changed_before_click_is_not_clicked(fake_reads):
    fake_reads['check']=False
    await probe.read_job_pages(manager(),'t')
    assert fake_reads['frame_clicks']==[] and fake_reads['capture']['recommend']['expanded']['reason']=='switch_control_changed'


async def test_menu_without_link_uses_one_native_click(fake_reads):
    fake_reads['menu']=[{'href':None,'path':'li.menu','x':10,'y':20,'hit':True}]
    m=manager()
    await probe.read_job_pages(m,'t')
    assert m.boss_session.page.clicks==[(10,20)] and m.boss_session.acts==['click']


async def test_ambiguous_menu_stops_and_returns_to_chat(fake_reads):
    fake_reads['menu']=[]
    m=manager()
    report=await probe.read_job_pages(m,'t')
    assert report['reason']=='job_menu_missing' and m.boss_session.navigations==[probe.CHAT_URL]


async def test_login_prompt_stops_without_further_navigation(fake_reads,monkeypatch):
    async def read_frames(session,kind):return session.page.url,[{'login':True}]
    monkeypatch.setattr(page_health,'read_frames',read_frames)
    m=manager()
    report=await probe.read_job_pages(m,'t')
    assert report['reason']=='login_required' and m.boss_session.navigations==['https://www.zhipin.com/web/chat/job/list']


async def test_challenge_instead_of_recommend_frame_stays_put(fake_reads,monkeypatch):
    async def read_frames(session,kind):
        return session.page.url,[{'challenge':session.page.url==probe.RECOMMEND_URL}]
    monkeypatch.setattr(page_health,'read_frames',read_frames)
    m=manager()
    original=m.boss_session.navigate
    async def navigate(url):
        await original(url)
        if url==probe.RECOMMEND_URL:m.boss_session.page.frames=[m.boss_session.page.main_frame]
    m.boss_session.navigate=navigate
    report=await probe.read_job_pages(m,'t')
    assert report['reason']=='verification_required' and m.boss_session.navigations[-1]==probe.RECOMMEND_URL


async def test_resume_during_probe_stops_without_returning(fake_reads):
    statuses=['paused']
    m=manager()
    m.account_execution=NS(members=lambda:[{'status':statuses[0]}])
    original=m.boss_session.navigate
    async def navigate(url):
        await original(url);statuses[0]='running'
    m.boss_session.navigate=navigate
    report=await probe.read_job_pages(m,'t')
    assert report['reason']=='not_permitted' and not report['returned_to_chat'] and len(m.boss_session.navigations)==1


@pytest.mark.parametrize('kw,message',[({'status':'running'},'暂停'),
    ({'owner':{'account_runner':True,'active_case_id':'c1'}},'安全点'),
    ({'owner':{'account_runner':True,'manual_takeover':True}},'接管'),
    ({'schedule':{'enabled':True,'timezone':'Asia/Shanghai','weekdays':[],'windows':[{'start':'09:00','end':'10:00'}]}},'执行时段')])
async def test_refuses_while_running_busy_or_outside_window(fake_reads,kw,message):
    m=manager(**kw)
    with pytest.raises(ValueError,match=message):await probe.read_job_pages(m,'t')
    assert m.boss_session.navigations==[]


@pytest.mark.parametrize('state,message',[({'reason':'draft_present','has_draft':True},'草稿'),({'reason':'unreadable','has_draft':False},'草稿')])
async def test_refuses_to_leave_chat_page_with_possible_draft(fake_reads,monkeypatch,state,message):
    async def check(session,kind):return {'healthy':False,**state}
    monkeypatch.setattr(page_health,'check',check)
    m=manager()
    with pytest.raises(ValueError,match=message):await probe.read_job_pages(m,'t')
    assert m.boss_session.navigations==[]


def test_capture_is_written_under_analysis(tmp_path):
    path=probe._write_capture(NS(root=tmp_path),{'a':'岗位甲'})
    assert path.startswith('analysis/job-pages-') and '岗位甲' in (tmp_path/path).read_text(encoding='utf-8')


def test_route_accepts_job_page_action():
    import inspect as pyinspect
    from cloakup import workbench
    assert "{'action':'read_job_pages'}" in pyinspect.getsource(workbench)


def test_skeleton_expression_is_bounded():
    expr=probe.skeleton_expression(['岗位甲'])
    assert str(probe.MAX_HTML) in expr and str(probe.MAX_NODES) in expr and '岗位甲' in expr


def test_ui_words_exclude_single_characters_that_double_as_names():
    assert not set(probe.UI_WORDS)&set('周万元天新月年中我个')


# 11-25 13:31 实采：职位列表 frame /web/frame/job_v2/list 刚出现时只有空的 <div id="app">，没等渲染就读了
async def test_job_list_waits_until_its_frame_has_content(fake_reads):
    fake_reads['frame_text']=[0,0,0,300]
    await probe.read_job_pages(manager(),'t')
    assert fake_reads['frame_text_reads']>=4 and fake_reads['capture']['job_list']['settled'] is True


async def test_job_list_never_rendering_is_recorded_not_fatal(fake_reads):
    fake_reads['frame_text']=[0]
    report=await probe.read_job_pages(manager(),'t')
    assert fake_reads['capture']['job_list']['settled'] is False and report['completed']
