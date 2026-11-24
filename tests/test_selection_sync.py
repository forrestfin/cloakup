"""Exercise the real inbox pass; recovery must verify its actual failed target."""
import hashlib
import json
import time
from types import SimpleNamespace as NS

import pytest

from cloakup import autopilot as ap, boss_chat, decision_runtime, resume_archive
from cloakup.sourcing import CaseStore


@pytest.fixture
def inbox(tmp_path,monkeypatch):
    store=CaseStore(tmp_path)
    case=store.create('https://www.zhipin.com/web/chat/job/edit?encryptId=test-job')
    store.bind_job(case,{'title':'测试岗位','requirements':'招聘经历'})
    rows=[];selected=[];decisions=[]
    def add(chat_id):
        candidate=store.add_candidate(case,'candidate:'+chat_id,'https://www.zhipin.com/web/chat/recommend',{'summary':'招聘经历'})
        candidate['conversation']={'chat_id':chat_id,'receiver':'测试候选人'+chat_id,'job_title':'测试岗位'}
        rows.append({'chat_id':chat_id,'text':'测试岗位','unread':False})
        store.save(case)
        return candidate
    async def menu(session):return rows.copy()
    async def open_chat(session,**binding):
        selected.append(binding['chat_id'])
        return {'valid':True,'chat_id':binding['chat_id'],'fingerprint':'verified:'+binding['chat_id'],
                'coverage':'当前加载','messages':[]}
    async def checkpoint(job):pass
    async def capture(*args):pass
    async def decide(manager,job,session,cid,pid,snapshot):decisions.append(pid)
    monkeypatch.setattr(boss_chat,'open_menu',menu)
    monkeypatch.setattr(boss_chat,'open_conversation',open_chat)
    monkeypatch.setattr(resume_archive,'capture',capture)
    monkeypatch.setattr(decision_runtime,'run_conversation',decide)
    manager=NS(cases=store,checkpoint=checkpoint,event=lambda *args:None)
    job={'id':'run','params':{'read_only':True}}
    return NS(store=store,case=case,add=add,rows=rows,selected=selected,decisions=decisions,manager=manager,job=job)


def incident(data,chat_id):
    data.job['selection_recovery']={data.case['id']:{'status':'retrying','attempts':1,'failures':[chat_id]}}


async def test_one_missing_row_does_not_block_replies_to_later_chats(inbox,monkeypatch):
    inbox.add('12-0');inbox.add('13-0')
    async def open_chat(session,**binding):
        inbox.selected.append(binding['chat_id'])
        if binding['chat_id']=='12-0':
            raise boss_chat.ChatSelectionUnavailable('当前列表没有唯一目标会话，未发送',stage='locate',reason='target_row_missing')
        return {'valid':True,'chat_id':binding['chat_id'],'fingerprint':'verified:'+binding['chat_id'],
                'coverage':'当前加载','messages':[]}
    monkeypatch.setattr(boss_chat,'open_conversation',open_chat)
    with pytest.raises(boss_chat.ChatSelectionUnavailable) as caught:
        await ap.sync_and_follow(inbox.manager,inbox.job,NS(),inbox.case)
    assert set(inbox.selected)=={'12-0','13-0'}
    assert caught.value.chat_id=='12-0' and caught.value.diagnostics['reason']=='target_row_missing'


async def test_first_selection_failure_ends_pass_without_scanning_remaining_25(inbox,monkeypatch):
    for i in range(1,27):inbox.add(f'{i}-0')
    async def fail(session,**binding):
        inbox.selected.append(binding['chat_id'])
        raise boss_chat.ChatSelectionUnavailable('会话选择超时',stage='click',reason='timeout',exception_type='TimeoutError')
    monkeypatch.setattr(boss_chat,'open_conversation',fail)
    with pytest.raises(boss_chat.ChatSelectionUnavailable) as caught:
        await ap.sync_and_follow(inbox.manager,inbox.job,NS(),inbox.case)
    assert inbox.selected==['1-0'] and caught.value.chat_id=='1-0'
    assert not inbox.decisions


async def test_failed_target_precedes_other_rows_and_bypasses_both_idle_caches(inbox):
    ordinary=inbox.add('12-0');failed=inbox.add('13-0')
    now=time.time()
    for person in (ordinary,failed):
        person.update(last_sync_at=now,decision_wait_until=now+9999,
            decision_wait_revision=inbox.case.get('config_revision',0),
            list_preview_hash=hashlib.sha256(json.dumps(['测试岗位',False],ensure_ascii=False).encode()).hexdigest())
    inbox.store.save(inbox.case)
    incident(inbox,'13-0');inbox.job['params']['read_only']=False
    count=await ap.sync_and_follow(inbox.manager,inbox.job,NS(),inbox.case)
    assert inbox.selected==['13-0'] and count==1
    assert inbox.decisions==[failed['id']]


async def test_original_failure_absent_from_list_cannot_pass_recovery(inbox,monkeypatch):
    inbox.add('12-0');inbox.add('13-0')
    inbox.rows[:]=[row for row in inbox.rows if row['chat_id']=='12-0']
    incident(inbox,'13-0')
    async def include(session,rows,chat_ids):
        assert '13-0' in chat_ids
        return rows
    monkeypatch.setattr(boss_chat,'include_chat_rows',include)
    with pytest.raises(boss_chat.ChatSelectionUnavailable) as caught:
        await ap.sync_and_follow(inbox.manager,inbox.job,NS(),inbox.case)
    assert inbox.selected==['12-0']
    assert caught.value.chat_id=='13-0' and caught.value.diagnostics['reason']=='target_missing'
    assert inbox.job['selection_recovery'][inbox.case['id']]['status']=='retrying'


async def test_read_only_success_returns_verified_count_and_syncs_history(inbox):
    first=inbox.add('12-0');second=inbox.add('13-0')
    inbox.rows.append({'chat_id':'99-0','text':'另一个岗位','unread':False})
    incident(inbox,'13-0')
    count=await ap.sync_and_follow(inbox.manager,inbox.job,NS(),inbox.case)
    assert inbox.selected==['13-0','12-0'] and count==2
    people=inbox.store.load(inbox.case['id'])['candidates']
    assert people[first['id']]['conversation_fingerprint']=='verified:12-0'
    assert people[second['id']]['conversation_fingerprint']=='verified:13-0'
    assert not inbox.decisions


async def test_unchanged_read_only_baseline_is_not_reopened(inbox):
    inbox.add('12-0')
    inbox.add('13-0')
    await ap.sync_and_follow(inbox.manager,inbox.job,NS(),inbox.case)
    assert inbox.selected==['12-0','13-0']
    await ap.sync_and_follow(inbox.manager,inbox.job,NS(),inbox.case)
    assert inbox.selected==['12-0','13-0']
    inbox.rows[1]['unread']=True
    await ap.sync_and_follow(inbox.manager,inbox.job,NS(),inbox.case)
    assert inbox.selected==['12-0','13-0','13-0']

    await ap.sync_and_follow(inbox.manager,inbox.job,NS(),inbox.case)
    assert inbox.selected==['12-0','13-0','13-0']
    inbox.rows[1]['text']='测试岗位 新回复'
    await ap.sync_and_follow(inbox.manager,inbox.job,NS(),inbox.case)
    assert inbox.selected==['12-0','13-0','13-0','13-0']


async def test_include_chat_rows_scrolls_until_the_offer_is_visible(monkeypatch):
    seen=[]
    async def evaluate(session,script):
        seen.append('scroll' if 'scrollTop' in script else 'rows')
        if 'scrollTop' in script:return {'moved':True,'end':False}
        return [{'chat_id':'12-0','text':'对方想发送附件简历给您','unread':True}] if seen.count('rows')==1 else []
    monkeypatch.setattr(boss_chat,'evaluate',evaluate)
    rows=await boss_chat.include_chat_rows(NS(),[{'chat_id':'9-0','text':'其他','unread':False}],['12-0'])
    assert [row['chat_id'] for row in rows]==['9-0','12-0']
    assert seen[0]=='scroll'


async def test_platform_control_resume_card_in_history_is_still_first(inbox,monkeypatch):
    waiting=inbox.add('12-0')
    offer=inbox.add('13-0')
    now=time.time()
    for person,row in ((waiting,inbox.rows[0]),(offer,inbox.rows[1])):
        digest=hashlib.sha256(json.dumps([row['text'],False],ensure_ascii=False).encode()).hexdigest()
        person.update(list_preview_hash=digest,last_sync_at=now)
    offer['history']=[{'direction':'in','platform_control':True,'external_id':'card',
                       'text':'对方想发送附件简历给您，您是否同意\n\n拒绝\n同意'}]
    inbox.rows[0]['unread']=True
    inbox.store.save(inbox.case)
    seen=[]
    async def include(session,rows,chat_ids):
        seen.append(chat_ids);return rows
    monkeypatch.setattr(boss_chat,'include_chat_rows',include)
    inbox.job['params']['read_only']=False
    await ap.sync_and_follow(inbox.manager,inbox.job,NS(),inbox.case)
    assert seen==[['13-0']]
    assert inbox.selected[0]=='13-0'
    assert inbox.decisions[0]==offer['id']


async def test_resume_offer_opens_before_other_unread(inbox):
    inbox.add('12-0')
    offer=inbox.add('13-0')
    inbox.rows[0]['unread']=True
    offer['inbox']=[{'external_id':'card','direction':'in','text':'对方想发送附件简历给您，您是否同意\n拒绝\n同意'}]
    inbox.store.save(inbox.case)
    inbox.job['params']['read_only']=False
    await ap.sync_and_follow(inbox.manager,inbox.job,NS(),inbox.case)
    assert inbox.selected[0]=='13-0'
    assert inbox.decisions[0]==offer['id']


async def test_offscreen_recovery_target_is_scrolled_in_before_other_rows(inbox,monkeypatch):
    inbox.add('12-0')
    inbox.add('13-0')
    inbox.rows[:]=[row for row in inbox.rows if row['chat_id']=='13-0']
    incident(inbox,'12-0')
    seen=[]
    async def include(session,rows,chat_ids):
        seen.append(chat_ids)
        return [{'chat_id':'12-0','text':'测试岗位','unread':False}]+rows
    monkeypatch.setattr(boss_chat,'include_chat_rows',include)
    await ap.sync_and_follow(inbox.manager,inbox.job,NS(),inbox.case)
    assert seen==[['12-0']]
    assert inbox.selected[0]=='12-0'


async def test_offscreen_resume_offer_is_included_before_other_rows(inbox,monkeypatch):
    offer=inbox.add('12-0')
    inbox.add('13-0')
    offer['inbox']=[{'external_id':'card','direction':'in','text':'对方想发送加密附件简历给您，您是否同意'}]
    inbox.rows[:]=[row for row in inbox.rows if row['chat_id']=='13-0']
    inbox.store.save(inbox.case)
    async def include(session,rows,chat_ids):
        assert chat_ids==['12-0']
        return [{'chat_id':'12-0','text':'对方想发送加密附件简历给您','unread':True}]+rows
    monkeypatch.setattr(boss_chat,'include_chat_rows',include)
    await ap.sync_and_follow(inbox.manager,inbox.job,NS(),inbox.case)
    assert inbox.selected[0]=='12-0'
    assert 'inbound_resume_cases' not in inbox.job or inbox.case['id'] not in inbox.job.get('inbound_resume_cases',[])


async def test_resume_offer_missing_from_list_defers_case(inbox,monkeypatch):
    offer=inbox.add('12-0')
    inbox.add('13-0')
    offer['inbox']=[{'external_id':'card','direction':'in','text':'对方想发送附件简历给您，您是否同意'}]
    inbox.rows[:]=[row for row in inbox.rows if row['chat_id']=='13-0']
    inbox.store.save(inbox.case)
    async def include(session,rows,chat_ids):return rows
    monkeypatch.setattr(boss_chat,'include_chat_rows',include)
    await ap.sync_and_follow(inbox.manager,inbox.job,NS(),inbox.case)
    assert '12-0' not in inbox.selected
    assert inbox.job['inbound_resume_cases']==[inbox.case['id']]


async def test_recovery_read_only_accepts_resume_offer_without_other_replies(inbox,monkeypatch):
    person=inbox.add('12-0')
    person['inbox']=[{'external_id':'card','direction':'in','text':'对方想发送附件简历给您，您是否同意 拒绝 同意'}]
    inbox.store.save(inbox.case)
    accepted=[]
    async def accept(manager,job,session,case,candidate,snapshot):accepted.append(candidate['id'])
    async def reread(session,**binding):
        return {'valid':True,'chat_id':binding['chat_id'],'fingerprint':'reread','coverage':'当前加载','messages':[]}
    monkeypatch.setattr('cloakup.boss_resume.accept_resume_request',accept)
    monkeypatch.setattr(boss_chat,'read_conversation',reread)
    inbox.job['params'].update(read_only=True,inbound_resume_accept=True)
    await ap.sync_and_follow(inbox.manager,inbox.job,NS(),inbox.case)
    assert accepted==[person['id']] and not inbox.decisions


async def test_plain_read_only_does_not_click_resume_accept(inbox,monkeypatch):
    person=inbox.add('12-0')
    person['inbox']=[{'external_id':'card','direction':'in','text':'对方想发送附件简历给您，您是否同意'}]
    inbox.store.save(inbox.case)
    async def accept(*args):raise AssertionError('只读通过不应点击同意')
    monkeypatch.setattr('cloakup.boss_resume.accept_resume_request',accept)
    await ap.sync_and_follow(inbox.manager,inbox.job,NS(),inbox.case)
    assert inbox.selected==['12-0'] and not inbox.decisions


async def test_encrypted_resume_offer_revisits_synced_chat_even_after_outbound_request(inbox):
    from cloakup.boss_resume import is_permission_text
    person=inbox.add('12-0')
    await ap.sync_and_follow(inbox.manager,inbox.job,NS(),inbox.case)
    case=inbox.store.load(inbox.case['id'])
    person=inbox.store.candidate(case,person['id'])
    text='对方想发送加密附件简历给您，您是否同意'
    assert is_permission_text(text)
    person['inbox'].append({'external_id':'permission','text':text})
    person['platform_actions']=[{'kind':'ask_resume','status':'awaiting_verification'}]
    inbox.store.save(case)
    await ap.sync_and_follow(inbox.manager,inbox.job,NS(),case)
    assert inbox.selected==['12-0','12-0']


async def test_platform_identity_is_written_back_to_the_same_chat(inbox,monkeypatch):
    person=inbox.add('12-0')
    async def open_chat(session,**binding):
        inbox.selected.append(binding['chat_id'])
        return {'valid':True,'chat_id':binding['chat_id'],'receiver':'平台姓名','job_title':'平台岗位',
                'fingerprint':'verified:'+binding['chat_id'],'coverage':'当前加载','messages':[]}
    monkeypatch.setattr(boss_chat,'open_conversation',open_chat)
    await ap.sync_and_follow(inbox.manager,inbox.job,NS(),inbox.case)
    saved=inbox.store.candidate(inbox.store.load(inbox.case['id']),person['id'])
    assert saved['conversation']['receiver']=='平台姓名'
    assert saved['conversation']['job_title']=='平台岗位'
    assert saved['profile']['platform_label']=='平台姓名'


def test_platform_identity_from_another_chat_is_not_written(inbox):
    person=inbox.add('12-0')
    case=inbox.store.load(inbox.case['id'])
    current=inbox.store.candidate(case,person['id'])
    updated=ap.adopt_platform_identity(inbox.store,case,current,{'valid':True,'chat_id':'99-0','receiver':'另一个人','job_title':'岗位'})
    assert updated['conversation']['receiver']=='测试候选人12-0'
    assert updated['conversation']['chat_id']=='12-0'


@pytest.mark.parametrize('due',[True,False])
async def test_due_deferred_chat_is_scrolled_in_for_recheck(inbox,monkeypatch,due):
    """隔离会话被虚拟列表滚出可视区时，到复核时间也要滚出来，否则永远等不到复核、岗位一直暂缓开聊。"""
    inbox.add('12-0')
    inbox.add('13-0')
    inbox.rows[:]=[row for row in inbox.rows if row['chat_id']=='13-0']
    case=inbox.store.load(inbox.case['id'])
    case['deferred_chat_ids']=['12-0'];case['deferred_chat_check_at']=time.time()+(-1 if due else 600)
    inbox.store.save(case)
    seen=[]
    async def include(session,rows,chat_ids):
        seen.append(list(chat_ids));return rows
    monkeypatch.setattr(boss_chat,'include_chat_rows',include)
    await ap.sync_and_follow(inbox.manager,inbox.job,NS(),inbox.store.load(inbox.case['id']))
    assert seen==[['12-0'] if due else []]


def _deferred(inbox,monkeypatch,guard=None):
    inbox.add('12-0')
    case=inbox.store.load(inbox.case['id'])
    case['deferred_chat_ids']=['12-0'];case['deferred_chat_check_at']=time.time()-1
    inbox.store.save(case)
    async def read_guard(page,js):return guard or {'editor_count':0,'has_draft':False,'login':False,'challenge':False,'chat_rows':1}
    monkeypatch.setattr(boss_chat,'_selection_page',lambda session:object())
    monkeypatch.setattr(boss_chat,'_bound_read',read_guard)
    logged=[]
    import cloakup.logbook as lb
    monkeypatch.setattr(lb,'log',lambda category,**fields:logged.append(fields))
    return logged


async def test_deferred_chat_is_rechecked_when_no_conversation_is_open(inbox,monkeypatch):
    """11-24：5 条隔离会话自 11-21 起没复核成功过。沟通页没打开会话时没有编辑框，复核被直接跳过。"""
    logged=_deferred(inbox,monkeypatch)
    await ap.sync_and_follow(inbox.manager,inbox.job,NS(),inbox.store.load(inbox.case['id']))
    case=inbox.store.load(inbox.case['id'])
    assert inbox.selected==['12-0'] and case['deferred_chat_ids']==[]
    assert any(f.get('type')=='deferred_chat_check' and f.get('result')=='verified' for f in logged)


@pytest.mark.parametrize('flag',['has_draft','login','challenge'])
async def test_deferred_recheck_still_stops_on_page_level_problems(inbox,monkeypatch,flag):
    logged=_deferred(inbox,monkeypatch,{'editor_count':1,'has_draft':False,'login':False,'challenge':False,flag:True})
    await ap.sync_and_follow(inbox.manager,inbox.job,NS(),inbox.store.load(inbox.case['id']))
    assert inbox.selected==[] and inbox.store.load(inbox.case['id'])['deferred_chat_ids']==['12-0']
    assert any(f.get('type')=='deferred_chat_check' and f.get('result')=='skipped' and f.get('reason')==flag for f in logged)


async def test_failed_deferred_recheck_is_counted_and_logged(inbox,monkeypatch):
    logged=_deferred(inbox,monkeypatch)
    async def open_chat(session,**binding):
        inbox.selected.append(binding['chat_id'])
        raise boss_chat.ChatSelectionUnavailable('没有唯一目标会话',stage='locate',reason='target_row_missing')
    monkeypatch.setattr(boss_chat,'open_conversation',open_chat)
    await ap.sync_and_follow(inbox.manager,inbox.job,NS(),inbox.store.load(inbox.case['id']))
    case=inbox.store.load(inbox.case['id'])
    assert case['deferred_chat_ids']==['12-0'] and case['deferred_chat_failures']['12-0']==1
    assert any(f.get('type')=='deferred_chat_check' and f.get('result')=='failed' and f.get('reason')=='target_row_missing' for f in logged)


async def test_chat_missing_at_the_bottom_three_times_stops_being_searched(inbox,monkeypatch):
    logged=_deferred(inbox,monkeypatch)
    async def open_chat(session,**binding):
        exc=boss_chat.ChatSelectionUnavailable('没有唯一目标会话',stage='locate',reason='target_row_missing')
        exc.diagnostics['list_end']=True
        raise exc
    monkeypatch.setattr(boss_chat,'open_conversation',open_chat)
    for _ in range(ap.MISSING_AFTER):
        case=inbox.store.load(inbox.case['id']);case['deferred_chat_check_at']=0;inbox.store.save(case)
        await ap.sync_and_follow(inbox.manager,inbox.job,NS(),inbox.store.load(inbox.case['id']))
    case=inbox.store.load(inbox.case['id'])
    assert case['deferred_chat_ids']==[] and case['missing_chat_ids']==['12-0']
    assert any(f.get('result')=='missing' for f in logged)
    # 会话重新出现在列表里：恢复正常处理。
    async def opened(session,**binding):
        return {'valid':True,'chat_id':binding['chat_id'],'fingerprint':'v','coverage':'当前加载','messages':[]}
    monkeypatch.setattr(boss_chat,'open_conversation',opened)
    await ap.sync_and_follow(inbox.manager,inbox.job,NS(),inbox.store.load(inbox.case['id']))
    assert inbox.store.load(inbox.case['id'])['missing_chat_ids']==[]


async def test_not_reaching_the_bottom_keeps_retrying(inbox,monkeypatch):
    _deferred(inbox,monkeypatch)
    async def open_chat(session,**binding):
        raise boss_chat.ChatSelectionUnavailable('没有唯一目标会话',stage='locate',reason='target_row_missing')
    monkeypatch.setattr(boss_chat,'open_conversation',open_chat)
    for _ in range(ap.MISSING_AFTER+1):
        case=inbox.store.load(inbox.case['id']);case['deferred_chat_check_at']=0;inbox.store.save(case)
        await ap.sync_and_follow(inbox.manager,inbox.job,NS(),inbox.store.load(inbox.case['id']))
    case=inbox.store.load(inbox.case['id'])
    assert case['deferred_chat_ids']==['12-0'] and not case.get('missing_chat_ids')
