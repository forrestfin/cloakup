import copy
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
import pytest
from test_agent_workspace import setup
from cloakup import greeting_policy as gp

pytestmark=pytest.mark.usefixtures('no_jitter')

@pytest.fixture
def env(setup):
 lab,m,t,c=setup;c['job']['url']='https://www.zhipin.com/web/chat/job/edit?encryptId=job1';c['desired_greeting']='您好，想和您交流招聘顾问这个机会。';c['platform_greeting_text']='旧话术';m.cases.save(c);t['config']['greeting']=c['desired_greeting'];m.db.put('task',t['id'],t)
 m.checkpoint=AsyncMock();m.event=lambda *a:None
 return m,{},NS(),m.cases.load(c['id'])

def snapshot(text):return {'enabled':True,'open_editor':False,'rows':[{'title':'招聘顾问','text':text,'edit_path':'#edit'},{'title':'其他岗位','text':'其他话术'}]}

async def test_live_platform_match_repairs_stale_local_cache_without_writing(env,monkeypatch):
 m,j,s,c=env;read=AsyncMock(return_value=snapshot(c['desired_greeting']));sync=AsyncMock();monkeypatch.setattr(gp,'read_settings',read);monkeypatch.setattr(gp,'synchronize',sync)
 assert await gp.ensure(m,j,s,c)
 sync.assert_not_awaited();assert m.cases.load(c['id'])['platform_greeting_text']==c['desired_greeting']
 assert m.db.list('greeting_readiness')[0]['status']=='verified'

async def test_real_difference_syncs_only_current_job_and_reads_back(env,monkeypatch):
 m,j,s,c=env;monkeypatch.setattr(gp,'read_settings',AsyncMock(side_effect=[snapshot('旧话术'),snapshot(c['desired_greeting'])]))
 async def sync(session,title,text,before,guard,before_save):assert title=='招聘顾问';await guard();before_save()
 spy=AsyncMock(side_effect=sync);monkeypatch.setattr(gp,'synchronize',spy)
 assert await gp.ensure(m,j,s,c);assert spy.await_count==1
 state=m.db.list('greeting_readiness')[0];assert not state['save_uncertain'] and state['actual_text']==c['desired_greeting']

async def test_unknown_save_is_read_only_across_repeated_attempts(env,monkeypatch):
 m,j,s,c=env;monkeypatch.setattr(gp,'read_settings',AsyncMock(return_value=snapshot('旧话术')))
 async def uncertain(*args):args[-1]();raise TimeoutError()
 spy=AsyncMock(side_effect=uncertain);monkeypatch.setattr(gp,'synchronize',spy)
 assert not await gp.ensure(m,j,s,c)
 for _ in range(2):
  record=m.db.list('greeting_readiness')[0];record['next_check_at']=0;m.db.put('greeting_readiness',record['id'],record)
  assert not await gp.ensure(m,j,s,c)
 assert spy.await_count==1 and m.db.list('greeting_readiness')[0]['save_uncertain']
 monkeypatch.setattr(gp,'read_settings',AsyncMock(return_value=snapshot(c['desired_greeting'])));record=m.db.list('greeting_readiness')[0];record['next_check_at']=0;m.db.put('greeting_readiness',record['id'],record)
 assert await gp.ensure(m,j,s,c);assert not m.db.list('greeting_readiness')[0]['save_uncertain']

async def test_duplicate_job_rows_and_disabled_settings_never_write(env,monkeypatch):
 m,j,s,c=env;spy=AsyncMock();monkeypatch.setattr(gp,'synchronize',spy)
 for state in [dict(snapshot('旧'),enabled=False),dict(snapshot('旧'),rows=[{'title':'招聘顾问','text':'旧'}]*2)]:
  monkeypatch.setattr(gp,'read_settings',AsyncMock(return_value=state))
  for r in m.db.list('greeting_readiness'):r['next_check_at']=0;m.db.put('greeting_readiness',r['id'],r)
  assert not await gp.ensure(m,j,s,c)
 spy.assert_not_awaited()

async def test_other_job_changes_fail_verification(env,monkeypatch):
 m,j,s,c=env;after=snapshot(c['desired_greeting']);after['rows'][1]['text']='被意外修改'
 monkeypatch.setattr(gp,'read_settings',AsyncMock(side_effect=[snapshot('旧'),after]));monkeypatch.setattr(gp,'synchronize',AsyncMock())
 assert not await gp.ensure(m,j,s,c);assert m.db.list('greeting_readiness')[0]['status']=='blocked'
 assert m.cases.load(c['id'])['platform_greeting_text']=='旧话术'

def test_parameter_rendering_is_per_job_and_rejects_unresolved_parameters():
 assert gp.render('您好，{{岗位名称}}',{'job':{'label':'岗位甲'}})=='您好，岗位甲'
 assert gp.render('您好，{{岗位名称}}',{'job':{'label':'岗位乙'}})=='您好，岗位乙'
 with pytest.raises(ValueError):gp.render('您好，{{未知参数}}',{'job':{'label':'岗位甲'}})
 with pytest.raises(Exception):gp.render('x'*100+'{{岗位名称}}',{'job':{'label':'岗位甲'}})

def test_greeting_todos_are_task_job_scoped_and_deduplicated(env):
 from cloakup.attention import AttentionStore
 m,j,s,c=env;t=m.definitions.list()[0];c['job_sourcing']['greeting']='您好';m.cases.save(c)
 ident=gp.key(t['id'],c['id']);m.db.put('greeting_readiness',ident,{'id':ident,'task_id':t['id'],'case_id':c['id'],'expected_hash':gp.stamp('您好'),'status':'blocked','reason':'未找到编辑控件'})
 inbox=AttentionStore(m.db);inbox.reconcile([],m.cases.list());inbox.reconcile([],m.cases.list());todos=[v for v in m.db.list('attention') if v['kind']=='greeting'];assert len(todos)==1 and todos[0]['task_id']==t['id']
 r=m.db.get('greeting_readiness',ident);r['status']='verified';m.db.put('greeting_readiness',ident,r);inbox.reconcile([],m.cases.list());assert m.db.get('attention',todos[0]['id'])['status']=='resolved'

async def test_one_job_block_does_not_stop_inboxes_or_next_job(env,monkeypatch):
 import asyncio
 from cloakup import autopilot as ap
 m,_,_,c=env;t=m.definitions.list()[0];other=copy.deepcopy(c);other['id']='other';other['job']['label']='岗位乙'
 store={c['id']:c,'other':other};calls=[]
 # 任务含两个岗位；每轮每个任务只开一批首聊，被阻断的岗位暂缓后，下一轮轮到另一个岗位。
 task=copy.deepcopy(t);task['config'].pop('members',None);task['config']['case_ids']=list(store);task['config'].setdefault('execution',{})['daily_new_limit']=20
 m.definitions=NS(get=lambda tid:task)
 m.cases=NS(load=lambda cid:copy.deepcopy(store[cid]),list=lambda:list(store.values()));m.account_execution=NS(members=lambda:[{'id':t['id'],'status':'running','case_ids':list(store)}],case_ids=lambda:list(store),can_send=lambda _:True,begin_work=lambda _:None,finish_cycle=lambda _:None)
 m.persist=lambda _:None
 async def sync(manager,job,session,case):calls.append('inbox:'+case['id']);return 1
 async def contact(manager,job,session,case):
  calls.append('new:'+case['id'])
  if case['id']==c['id']:raise gp.GreetingUnavailable('测试话术差异')
  return 'contacted'
 job={'id':'run','params':{'new_contacts':True}}
 async def end(*a):
  if job.get('autopilot_cycle',0)>=2:raise asyncio.CancelledError()
 monkeypatch.setattr(ap,'sync_and_follow',sync);monkeypatch.setattr(ap,'first_contact',contact);monkeypatch.setattr(ap.asyncio,'sleep',end)
 # 每批提交次数在生产中随机抽取；此处固定取区间上限，只验证岗位阻断互不影响。
 monkeypatch.setattr(ap.random,'randint',lambda lo,hi:hi)
 with pytest.raises(asyncio.CancelledError):await ap.run_account_executor(m,job,NS())
 assert calls[:2]==['inbox:'+c['id'],'inbox:other']
 assert sorted(x for x in calls if x.startswith('new:'))==sorted(['new:'+c['id'],*(['new:other']*3)])
 assert m.db.list('greeting_readiness')[0]['status']=='blocked'

async def test_native_sync_checks_dialog_job_and_saves_once(monkeypatch):
 from cloakup import frame_actions
 monkeypatch.setattr(frame_actions,'click',AsyncMock())
 loc=NS(count=AsyncMock(return_value=1),click=AsyncMock());frame=NS(url=gp.GREETING_FRAME,locator=lambda _:loc)
 session=NS(page=NS(frames=[frame]),type_text=AsyncMock())
 async def act(kind,fn):assert kind=='click';return await fn()
 session._act=act
 monkeypatch.setattr(gp,'_frame_value',AsyncMock(side_effect=[['招聘顾问'],False]));monkeypatch.setattr(gp,'_find',AsyncMock(return_value=(NS(index=1,raw={'value':'新话术'}),None)));click=AsyncMock();monkeypatch.setattr(gp,'_click',click)
 guard=AsyncMock();saved=[]
 await gp.synchronize(session,'招聘顾问','新话术',snapshot('旧'),guard,lambda:saved.append(True))
 assert len(saved)==1 and session.type_text.await_count==1 and click.await_count==1
 assert click.await_args.args[1]=='保存'

async def test_wrong_dialog_job_never_types_or_saves(monkeypatch):
 from cloakup import frame_actions
 monkeypatch.setattr(frame_actions,'click',AsyncMock())
 loc=NS(count=AsyncMock(return_value=1),click=AsyncMock());session=NS(page=NS(frames=[NS(url=gp.GREETING_FRAME,locator=lambda _:loc)]),type_text=AsyncMock())
 async def act(kind,fn):return await fn()
 session._act=act;monkeypatch.setattr(gp,'_frame_value',AsyncMock(return_value=['其他岗位']));click=AsyncMock();monkeypatch.setattr(gp,'_click',click)
 with pytest.raises(gp.GreetingUnavailable):await gp.synchronize(session,'招聘顾问','新话术',snapshot('旧'),AsyncMock(),lambda:None)
 session.type_text.assert_not_awaited();click.assert_not_awaited()


async def test_settings_wait_for_job_rows_before_treating_the_job_as_missing(monkeypatch):
 # 11-24 15:44／15:57：重启后话术设置页的岗位列表还没画出来就读，读到 0 行，误判本岗位没有话术，
 # 转去“新增话术”又发现岗位已存在，只好暂停本岗位开聊 10 分钟。读岗位列表前先等它出现。
 rows=[{'title':'招聘顾问','text':'话术','edit_path':'#edit'}];reads=iter([[],[],rows])
 async def value(session,frag,expr):
  if expr==gp.READ_ROWS:return next(reads)
  return 'textarea' not in expr
 async def no_sleep(*a):pass
 monkeypatch.setattr(gp,'_frame_value',value);monkeypatch.setattr(gp.asyncio,'sleep',no_sleep)
 session=NS(page=NS(url=gp.GREETING_URL,frames=[NS(url='https://www.zhipin.com'+gp.GREETING_FRAME)]))
 assert (await gp.read_settings(session))['rows']==rows


async def test_settings_wait_for_the_job_tab_before_giving_up(monkeypatch):
 # 11-24 16:23：导航后 7 秒内页签还没画出来，立即判“无法唯一核实按岗位设置页签”，开聊白停 10 分钟。
 import cloakup.frame_actions as fa
 tabs=iter([None,None,'#tab']);clicked=[]
 rows=[{'title':'招聘顾问','text':'话术','edit_path':'#edit'}]
 async def value(session,frag,expr):
  if expr==gp.READ_ROWS:return rows
  if 'job-name' in expr:return bool(clicked)
  return 'textarea' not in expr
 async def text_control(page,frame,label):return next(tabs)
 async def click(session,frame,css):clicked.append(css)
 async def no_sleep(*a):pass
 monkeypatch.setattr(gp,'_frame_value',value);monkeypatch.setattr(gp.asyncio,'sleep',no_sleep)
 monkeypatch.setattr(fa,'text_control',text_control);monkeypatch.setattr(fa,'click',click)
 session=NS(page=NS(url=gp.GREETING_URL,frames=[NS(url='https://www.zhipin.com'+gp.GREETING_FRAME)]))
 assert (await gp.read_settings(session))['rows']==rows and clicked==['#tab']
