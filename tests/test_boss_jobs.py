"""11-25 Task 7：从 BOSS 读已发布岗位。id 只取推荐页岗位下拉（value=encryptJobId），职位管理列表按名称补城市与状态。"""
from types import SimpleNamespace as NS

import pytest

from cloakup import boss_jobs as bj
from cloakup import page_health
from cloakup.sourcing import SourcingError

ID_A='abcDEF123456~~';ID_B='xyzXYZ987654__'


class Frame:
    def __init__(self,url):self.url=url


class Session:
    def __init__(self):
        self.page=NS(url=bj.CHAT_URL,frames=[],main_frame=Frame(bj.CHAT_URL));self.navigations=[]
        self._boss_recommend_list={'stale':True};self._boss_recommend_detail={'stale':True}
    async def navigate(self,url):
        self.navigations.append(url);self.page.url=url
        if url==bj.RECOMMEND_URL:self.page.frames=[self.page.main_frame,Frame('https://www.zhipin.com/web/frame/recommend/?jobid='+ID_A+'&status=0')]
        elif url==bj.JOB_LIST_URL:self.page.frames=[self.page.main_frame,Frame('https://www.zhipin.com/web/frame/job_v2/list?jobversion=2')]
        else:self.page.frames=[self.page.main_frame]


@pytest.fixture
def page(monkeypatch):
    calls={'clicks':[],'heal':{'healthy':True,'reason':None},'guard':{'login':False,'challenge':False},
           'options':[{'value':ID_A,'text':'数据顾问 上海 15-25K','current':True},{'value':'bad"id','text':'坏选项','current':False}],
           'rows':{'total':2,'paged':False,'rows':[{'title':'数据顾问','labels':['上海','浦东','3-5年'],'status_class':'status-box status-opening','status_text':'开放中'},
                                                  {'title':'交付顾问','labels':['北京'],'status_class':'status-box status-opening','status_text':'开放中'}]}}
    async def heal(session,kind):calls['heal_kind']=kind;return calls['heal']
    async def read_frames(session,kind):return session.page.url,[calls['guard']]
    async def frame_click(session,frame,css):calls['clicks'].append(css)
    async def frame_value(session,fragment,expression):
        assert fragment==bj.RECOMMEND_FRAME
        return calls['options']
    async def read_frame(session,frame,expression):
        if expression==bj.TEXT_LEN:return 500
        return calls['rows']
    async def no_sleep(_):return None
    monkeypatch.setattr(page_health,'heal',heal);monkeypatch.setattr(page_health,'read_frames',read_frames)
    monkeypatch.setattr(bj,'_frame_click',frame_click);monkeypatch.setattr(bj,'_frame_value',frame_value);monkeypatch.setattr(bj,'_read_frame',read_frame)
    monkeypatch.setattr(bj.asyncio,'sleep',no_sleep)
    return calls


def test_merge_takes_ids_from_dropdown_and_marks_list_only_rows_not_recommendable():
    options=[{'encrypt_id':ID_A,'text':'数据顾问 上海 15-25K'}]
    rows=[{'title':'数据顾问','detail':'上海','status':'open','status_text':'开放中'},{'title':'交付顾问','detail':'北京','status':'open','status_text':'开放中'}]
    jobs=bj.merge(options,rows)
    assert jobs[0]=={'encrypt_id':ID_A,'title':'数据顾问','detail':'上海','status':'open','status_text':'开放中','recommendable':True,'option_text':'数据顾问 上海 15-25K'}
    assert jobs[1]['encrypt_id'] is None and jobs[1]['recommendable'] is False and jobs[1]['title']=='交付顾问'


def test_merge_prefers_the_longest_title_and_keeps_dropdown_only_options():
    # 名称互为前缀时按最长岗位名对应；下拉里有、当前页列表里没有的岗位仍可绑定（列表可能被截断）。
    options=[{'encrypt_id':ID_A,'text':'顾问助理 上海'},{'encrypt_id':ID_B,'text':'运营专员 杭州'}]
    rows=[{'title':'顾问','detail':'北京','status':'open','status_text':'开放中'},{'title':'顾问助理','detail':'上海','status':'closed','status_text':'已关闭'}]
    jobs={j['title']:j for j in bj.merge(options,rows)}
    assert jobs['顾问助理']['encrypt_id']==ID_A and jobs['顾问']['encrypt_id'] is None
    assert jobs['运营专员']['encrypt_id']==ID_B and jobs['运营专员']['recommendable'] and jobs['运营专员']['detail'] is None


def test_parse_rows_keeps_leading_labels_as_detail_not_city():
    # 11-25 16:30 线上首读：列表第一个标签是公司名（“君润人力”），被当成城市显示；改为原样显示前三个标签。
    rows=bj.parse_rows([{'title':' 数据顾问 ','labels':['某公司','上海','浦东','3-5年','本科'],'status_class':'status-box status-opening','status_text':'开放中'},
                        {'title':'交付顾问','labels':[],'status_class':'status-box','status_text':'已关闭'},
                        {'title':'','labels':['北京'],'status_class':'','status_text':''},
                        {'title':'测试岗','labels':['深圳'],'status_class':'status-box status-wait','status_text':'待开放'}])
    assert [(r['title'],r['detail'],r['status']) for r in rows]==[('数据顾问','某公司 · 上海 · 浦东','open'),('交付顾问',None,'closed'),('测试岗','深圳','other')]
    assert all('city' not in r for r in rows)


async def test_dropdown_is_opened_once_and_no_option_is_chosen(page):
    # 11-25 管理员确认：点开岗位下拉一次可以，不选选项；value 不合规的选项丢弃。
    session=Session()
    options=await bj.read_recommend_options(session)
    assert page['clicks']==[bj.SWITCH_LABEL] and page['heal_kind']=='recommend'
    assert options==[{'encrypt_id':ID_A,'text':'数据顾问 上海 15-25K','current':True}]


async def test_login_on_recommend_page_is_reported_as_page_level(page):
    page['heal']={'healthy':False,'reason':'login_required'}
    with pytest.raises(SourcingError,match='登录'):await bj.read_recommend_options(Session())
    assert page['clicks']==[]


async def test_job_rows_are_capped_and_truncation_is_flagged(page):
    page['rows']={'total':60,'paged':False,'rows':[{'title':'岗位'+str(i),'labels':['上海'],'status_class':'status-opening','status_text':'开放中'} for i in range(60)]}
    result=await bj.read_job_rows(Session())
    assert len(result['rows'])==bj.MAX_ROWS and result['truncated'] is True


async def test_verification_on_job_list_stops_before_reading(page):
    page['guard']={'login':False,'challenge':True}
    with pytest.raises(SourcingError,match='校验'):await bj.read_job_rows(Session())


async def test_read_published_merges_and_returns_to_chat(page):
    session=Session()
    result=await bj.read_published(session)
    assert session.navigations[-1]==bj.CHAT_URL and session._boss_recommend_list is None
    assert [j['title'] for j in result['jobs']]==['数据顾问','交付顾问'] and result['jobs'][0]['encrypt_id']==ID_A
    assert result['truncated'] is False and result['read_at']>0


async def test_read_published_does_not_navigate_after_login_loss(page):
    page['heal']={'healthy':False,'reason':'login_required'}
    session=Session()
    with pytest.raises(SourcingError,match='登录'):await bj.read_published(session)
    assert bj.CHAT_URL not in session.navigations


def test_bind_url_passes_job_url_validation():
    from cloakup.boss import validate_job_url
    assert validate_job_url(bj.job_url(ID_A))==bj.job_url(ID_A)
    with pytest.raises(ValueError):bj.job_url('bad"id')


def test_merge_requires_a_separator_after_the_title_and_never_drops_options():
    # 11-25 审阅：选项文字是“岗位名 + 空格 + 城市／薪资”；“Java开发 上海”不能算到“Java”头上，也不能被吞掉。
    options=[{'encrypt_id':ID_A,'text':'Java开发 上海 15-25K'},{'encrypt_id':ID_B,'text':'Java 北京 10-15K'}]
    rows=[{'title':'Java','detail':'北京','status':'open','status_text':'开放中'}]
    jobs={j['title']:j for j in bj.merge(options,rows)}
    assert jobs['Java']['encrypt_id']==ID_B and jobs['Java开发']['encrypt_id']==ID_A and jobs['Java开发']['recommendable']
    assert jobs['Java开发']['option_text']=='Java开发 上海 15-25K' and jobs['Java开发']['detail'] is None


def test_row_matched_by_two_options_gets_no_id_and_both_options_stay_listed():
    # 11-25 审阅：一行对上两个选项（无法确定是哪个），这一行不给 id，两个选项都作为下拉岗位列出。
    options=[{'encrypt_id':ID_A,'text':'数据顾问 上海'},{'encrypt_id':ID_B,'text':'数据顾问 北京'}]
    rows=[{'title':'数据顾问','detail':'上海','status':'open','status_text':'开放中'}]
    jobs=bj.merge(options,rows)
    assert jobs[0]['title']=='数据顾问' and jobs[0]['encrypt_id'] is None and not jobs[0]['recommendable']
    assert sorted(j['encrypt_id'] for j in jobs[1:])==sorted([ID_A,ID_B]) and all(j['recommendable'] for j in jobs[1:])


def test_truncated_list_keeps_dropdown_only_jobs_bindable():
    # 11-25 审阅：列表只读了第一页时，下拉里有、列表里没有的岗位照样列出可绑定。
    jobs=bj.merge([{'encrypt_id':ID_A,'text':'运营专员 杭州 8-12K'}],[{'title':'数据顾问','detail':'上海','status':'open','status_text':''}])
    extra=next(j for j in jobs if j['encrypt_id']==ID_A)
    assert extra['title']=='运营专员' and extra['recommendable']
