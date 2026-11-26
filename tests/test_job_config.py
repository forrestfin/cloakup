import copy
from types import SimpleNamespace as NS
import pytest
from cloakup.sourcing import CaseStore,SourcingError
from cloakup.job_config import view_config,save_config,locked_task,locked_tasks
from cloakup.autonomy import local_brief
from cloakup import autopilot as ap

@pytest.fixture
def data(tmp_path):
 s=CaseStore(tmp_path);c=s.create('https://www.zhipin.com/web/chat/job/edit?encryptId=j1')
 s.bind_job(c,{'title':'招聘顾问','requirements':'原始招聘要求'})
 c['anonymous_job']={'role':'招聘顾问','requirements':['原始招聘要求']}
 s.add_candidate(c,'p1','https://www.zhipin.com/web/chat/recommend',{'experience':'5年','education':'本科'})
 s.set_autonomy(c,True,source='user')
 c['greeting_draft']='您好，方便聊一下吗？';s.save(c)
 return s,c

def body(c):
 cfg=view_config(c);rev=cfg.pop('revision');return {'expected_revision':rev,'config':cfg}

def test_edit_versions_effective_job_and_invalidates_old_message(data):
 s,c=data;m=s.draft(c,'p1','follow_up','旧消息');s.authorize_automation(c,'p1',m['id'],decision={'reply':'x'})
 b=body(c);b['config']['jd']='修改后的要求';b['config']['anonymous_job']={'role':'招聘顾问','requirements':['更新后的匿名要求']}
 b['config']['execution']['daily_new_limit']=3
 save_config(s,c,b)
 assert c['config_revision']==1 and c['source_job_snapshot']['requirements']=='原始招聘要求'
 assert c['job']['snapshot']['requirements']=='修改后的要求' and m['status']=='stale'
 assert c['autonomy']['daily_new_limit']==3 and c['config_history'][0]['revision']==0
 with pytest.raises(SourcingError):s.claim_send(c,'p1',m['id'])
 with pytest.raises(SourcingError):save_config(s,c,b)
 s.set_autonomy(c,False,source='user');s.set_autonomy(c,True,source='user')
 assert c['autonomy']['daily_new_limit']==3

def test_new_jd_cannot_reuse_old_anonymous_model_facts(data):
 s,c=data;b=body(c);b['config']['jd']='新JD';save_config(s,c,b)
 assert c['anonymous_job'] is None
 with pytest.raises(SourcingError):local_brief(c,c['candidates']['p1'])

def test_invalid_edit_or_inflight_send_does_not_partially_save(data):
 s,c=data;old=copy.deepcopy(c);b=body(c);b['config']['execution']['daily_new_limit']=200
 with pytest.raises(SourcingError):save_config(s,c,b)
 assert c==old
 m=s.draft(c,'p1','follow_up','消息');s.authorize_automation(c,'p1',m['id'],decision={'reply':'x'});s.claim_send(c,'p1',m['id'])
 with pytest.raises(SourcingError):save_config(s,c,body(c))
 assert not c.get('config_revision')

async def test_multiple_jobs_use_one_session_and_isolate_case_failure(data,monkeypatch):
 s,c=data;c2=s.create('https://www.zhipin.com/web/chat/job/edit?encryptId=j2');s.bind_job(c2,{'title':'另一岗位','requirements':'经验'})
 s.set_autonomy(c2,True,source='user');seen=[];session=object()
 async def run(m,j,ss):
  assert ss is session;seen.append(j['params']['case_id'])
  if seen[-1]==c['id']:raise SourcingError('岗位配置需核对')
  return {'status':'succeeded','summary':'done'}
 monkeypatch.setattr(ap,'_run_single',run)
 class Manager:
  cases=s
  def persist(self,*a):pass
  async def checkpoint(self,*a):pass
 job={'id':'run','params':{'case_id':c['id'],'case_ids':[c['id'],c2['id']],'continuous':False},'events':[]}
 r=await ap.run_autopilot(Manager(),job,session)
 assert seen==[c['id'],c2['id']] and job['case_results'][c['id']]['status']=='held'
 assert job['case_results'][c2['id']]['status']=='succeeded' and len(job['params']['case_ids'])==2


def test_confirmed_faq_requeues_only_latest_pending_question(data):
 s,c=data;s.receive(c,'p1','q1','需要坐班吗？')
 p=c['candidates']['p1'];p.update(handled_reply_id='q1',pending_question_id='q1');s.save(c)
 b=body(c);b['config']['faq']=[{'question':'需要坐班吗？','answer':'该岗位支持远程协作。','variants':[],'enabled':True}]
 save_config(s,c,b)
 assert p.get('handled_reply_id') is None and view_config(c)['faq'][0]['enabled']


def test_typed_facts_edit_is_versioned_and_old_jd_invalidates_unchanged_facts(data):
 s,c=data;c['anonymous_job']={'role':'招聘顾问','requirements':['远程协作。']};s.save(c)
 b=body(c);assert b['config']['facts'][0]['kind']=='work_arrangement'
 b['config']['facts'][0]['value']='工作地点在武汉。';save_config(s,c,b)
 fact=c['confirmed_facts'][0];assert fact['source']=='administrator' and fact['revision']==1
 b=body(c);b['config']['jd']='新的JD';save_config(s,c,b)
 assert c['confirmed_facts'][0]['status']=='unconfirmed'


@pytest.mark.parametrize('requirements,kinds',[
 (['Java 后端开发经验 3 年以上，本科及以上学历。','负责交易系统的设计与开发。','有金融科技背景优先。'],
  {0:{'experience','education'},1:{'duties'},2:{'requirements'}}),
 (['招聘或猎头从业经验至少五年，甲方招聘或乙方猎头背景均可。','只负责寻访触达也可商议。'],
  {0:{'experience','requirements'},1:{'duties'}}),
])
def test_fact_anchors_are_job_agnostic(requirements,kinds):
 # 事实锚点不内置岗位词：不同 JD 用同一套通用句式识别事实类型。
 from cloakup.facts import facts_for
 found={}
 for f in facts_for({'anonymous_job':{'role':'岗位','requirements':requirements}}):found.setdefault(f['anonymous_index'],set()).add(f['kind'])
 assert found==kinds

def test_locked_task_matches_save_config(data):
 # 11-25 岗位页与任务「岗位与事实」两处可改：锁定条件须与 save_config 拒绝保存的条件一致
 s,c=data;assert locked_task(s.db,c['id']) is None
 save_config(s,c,body(c))
 s.db.put('task','t1',{'id':'t1','config':{'name':'多岗位寻访','engine':'boss_sourcing','case_ids':[c['id']]}})
 assert locked_task(s.db,c['id']) is None
 save_config(s,c,body(c))
 s.db.put('agent_release','t1',{'id':'t1','version_id':'v1'})
 assert locked_task(s.db,c['id'])=={'id':'t1','name':'多岗位寻访'}
 with pytest.raises(SourcingError,match='^该岗位已纳入验证后发布，请在关联任务的岗位资料与寻访配置中修改并验证，不能直接覆盖生效事实$'):save_config(s,c,body(c))

def test_locked_tasks_batch_matches_locked_task(data):
 # 11-25 状态轮询批量算锁定：结果须与逐个 locked_task 一致（无发布／已锁定／未锁定）
 s,c=data;c2=s.create('https://www.zhipin.com/web/chat/job/edit?encryptId=j2');s.bind_job(c2,{'title':'另一岗位','requirements':'经验'})
 ids=[c['id'],c2['id']]
 s.db.put('task','t1',{'id':'t1','config':{'name':'多岗位寻访','engine':'boss_sourcing','case_ids':[c['id']]}})
 assert locked_tasks(s.db,ids)=={cid:locked_task(s.db,cid) for cid in ids}=={c['id']:None,c2['id']:None}
 s.db.put('agent_release','t1',{'id':'t1','version_id':'v1'})
 assert locked_tasks(s.db,ids)=={cid:locked_task(s.db,cid) for cid in ids}=={c['id']:{'id':'t1','name':'多岗位寻访'},c2['id']:None}
 reads=[];real=s.db.list
 s.db.list=lambda kind:(reads.append(kind),real(kind))[1]
 locked_tasks(s.db,ids);assert sorted(reads)==['account_task','agent_release','task']
 s.db.list=real
