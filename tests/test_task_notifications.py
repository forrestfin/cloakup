import copy
import json
import time
from types import SimpleNamespace as NS
import pytest
from cloakup import task_notifications as n
from cloakup.storage import Database
from cloakup.sourcing import CaseStore
from cloakup.resume_archive import save_file


class Definitions:
 def __init__(self,db):self.db=db
 def list(self):return self.db.list('task')
 def get(self,key):return self.db.get('task',key)

class Client:
 def __init__(self,fail=None):self.calls=[];self.fail=fail;self.uploads=0
 def configured(self):return True
 def authenticate(self):return 'memory-only-token'
 def upload(self,record,data):self.uploads+=1;assert data.startswith(b'%PDF-');return 'file_key_private'
 def send(self,receiver,receiver_type,msg_type,content,stable_uuid):
  self.calls.append((receiver,receiver_type,msg_type,content,stable_uuid))
  if self.fail and len(self.calls)==self.fail[0]:raise self.fail[1]
  return 'om_'+str(len(self.calls))

@pytest.fixture
def env(tmp_path):
 db=Database(tmp_path);cases=CaseStore(tmp_path,db)
 c=cases.create('https://www.zhipin.com/web/chat/job/edit?encryptId=test')
 cases.bind_job(c,{'title':'技术招聘','requirements':'招聘经验'})
 p=cases.add_candidate(c,'person','https://www.zhipin.com/web/chat/index',{'summary':'职业资料','platform_label':'测试候选人'})
 record=save_file(cases,c,p,'platform-message:file',b'%PDF-test-only','测试简历.pdf')
 task={'id':'task1','config':{'name':'高端岗位 · 寻访','case_ids':[c['id']],'notifications':{'enabled':True,'resume_chat_id':'oc_group','todo_user_id':'ou_admin'}}}
 db.put('task',task['id'],task)
 db.put('attention','todo1',{'id':'todo1','kind':'question','created_at':1,'history':[],'case_id':c['id'],'status':'pending','title':'候选人问题需确认','question':'具体社保安排是什么？'})
 manager=NS(db=db,cases=cases,definitions=Definitions(db),jobs={},_notification_client=Client())
 return manager,task,c,p,record


def test_notification_settings_validate_destinations_not_credentials():
 assert n.validate_config()==n.DEFAULTS
 for cfg in [{'enabled':True},{'enabled':True,'resume_chat_id':'https://evil.test'},{'enabled':True,'todo_user_id':'oc_group'},{'app_secret':'never'}]:
  with pytest.raises(ValueError):n.validate_config(cfg)


async def test_file_and_todo_delivery_are_bound_durable_and_idempotent(env):
 m,task,c,p,record=env
 unrelated=copy.deepcopy(task);unrelated.update(id='other');unrelated['config']['case_ids']=[];m.db.put('task','other',unrelated)
 await n.tick(m);await n.tick(m);await n.tick(m)
 rows=m.db.list(n.KIND);assert len(rows)==2 and all(r['status']=='sent' for r in rows)
 assert all(r['task_id']=='task1' for r in rows)
 assert [c[0] for c in m._notification_client.calls]==['oc_group','oc_group','ou_admin']
 assert m._notification_client.calls[0][2]=='file' and m._notification_client.calls[2][2]=='text'
 assert '技术招聘' in m._notification_client.calls[1][3]['text']
 assert len({c[4] for c in m._notification_client.calls})==3
 # Another reference to the same bytes does not send another copy.
 save_file(m.cases,c,p,'platform-message:file-duplicate',b'%PDF-test-only','重复简历.pdf')
 await n.tick(m)
 assert len(m._notification_client.calls)==3


async def test_unknown_response_is_never_replayed_even_after_restart(env):
 m,task,c,p,record=env
 m._notification_client=Client((1,n.DeliveryError('response lost',ambiguous=True)))
 await n.tick(m)
 r=next(r for r in m.db.list(n.KIND) if r['kind']=='resume');assert r['status']=='unknown'
 delattr(m,'_notification_lock');m._notification_client=Client()
 await n.tick(m);await n.tick(m)
 assert all(call[0]!='oc_group' for call in m._notification_client.calls)


async def test_explicit_rejection_retry_keeps_stable_uuid_and_file_receipt(env):
 m,task,c,p,record=env
 m._notification_client=Client((2,n.DeliveryError('rate limited',retryable=True)))
 await n.tick(m)
 row=next(r for r in m.db.list(n.KIND) if r['kind']=='resume')
 assert row['status']=='retry' and row['file_message_id']=='om_1'
 row['next_attempt_at']=0;n._save(m.db,row)
 n._process(m,m._notification_client,row)
 assert len(m._notification_client.calls)==3
 assert m._notification_client.calls[1][4]==m._notification_client.calls[2][4]
 assert m._notification_client.uploads==1
 assert m.db.get(n.KIND,row['id'])['status']=='sent'


async def test_disabled_or_resolved_events_never_send(env):
 m,task,c,p,record=env;n.enqueue(m)
 task['config']['notifications']['enabled']=False;m.db.put('task',task['id'],task)
 await n.tick(m);await n.tick(m)
 assert not m._notification_client.calls
 assert all(r['status']=='cancelled' for r in m.db.list(n.KIND))


async def test_restarting_during_send_records_unknown(env):
 m,task,c,p,record=env;n.enqueue(m)
 row=next(r for r in m.db.list(n.KIND) if r['kind']=='resume');row['status']='sending';n._save(m.db,row)
 await n.tick(m)
 assert m.db.get(n.KIND,row['id'])['status']=='unknown' and not m._notification_client.calls


def test_status_redacts_keys_content_and_credentials(env,monkeypatch):
 m,task,c,p,record=env;n.enqueue(m)
 row=m.db.list(n.KIND)[0];row['file_key']='private-upload-key';n._save(m.db,row)
 monkeypatch.setenv('LARK_APP_ID','cli_private');monkeypatch.setenv('LARK_APP_SECRET','secret-never-output')
 out=json.dumps(n.status(m),ensure_ascii=False)
 for value in ('private-upload-key','secret-never-output','cli_private','测试候选人'):assert value not in out
 assert n.status(m)['configured']


def test_account_owner_todo_routes_only_members(env):
 m,task,c,p,record=env;m.jobs={'run':{'account_runner':True}}
 todo={'id':'login','status':'pending','run_id':'run','title':'重新登录'}
 assert not n._owns_todo(m,task,todo)
 m.db.put('account_task','task1',{'id':'task1','status':'running','case_ids':[c['id']]})
 assert n._owns_todo(m,task,todo)
 assert not n._owns_todo(m,{'id':'other','config':{'case_ids':[]}},todo)


def test_feishu_auth_and_message_use_server_token_and_stable_uuid(monkeypatch):
 client=n.FeishuClient();calls=[]
 monkeypatch.setenv('LARK_APP_ID','cli_test');monkeypatch.setenv('LARK_APP_SECRET','secret')
 def post(url,data,headers,**kwargs):
  calls.append((url,json.loads(data),headers))
  if 'oauth/v3' in url:return {'code':0,'access_token':'memory-token','expires_in':7200}
  return {'code':0,'data':{'message_id':'om_test'}}
 client._post=post
 assert client.send('oc_group','chat_id','text',{'text':'task event'},'stable-uuid')=='om_test'
 client.send('oc_group','chat_id','text',{'text':'task event'},'stable-uuid')
 assert len(calls)==3 and calls[1][1]['uuid']==calls[2][1]['uuid']=='stable-uuid'
 assert calls[1][2]['Authorization']=='Bearer memory-token'
 assert 'client_secret' not in calls[1][1]


def test_http400_sending_in_progress_is_not_treated_as_rejected_retry():
 import io
 from urllib.error import HTTPError
 client=n.FeishuClient()
 class Opener:
  def open(self,req,timeout):
   raise HTTPError(req.full_url,400,'error',{},io.BytesIO(b'{"code":230049,"msg":"message is being sent; private data"}'))
 client.opener=Opener()
 with pytest.raises(n.DeliveryError) as result:
  client._post('https://open.feishu.cn/open-apis/im/v1/messages',b'{}',{},message=True)
 assert result.value.ambiguous and not result.value.retryable
 assert 'private data' not in str(result.value)


async def test_missing_credentials_waits_without_network_and_resumes_after_setup(env,monkeypatch):
 m,task,c,p,record=env
 monkeypatch.delenv('LARK_APP_ID',raising=False);monkeypatch.delenv('LARK_APP_SECRET',raising=False)
 m._notification_client=Client();m._notification_client.configured=lambda:False
 await n.tick(m);await n.tick(m)
 rows=m.db.list(n.KIND)
 assert len(rows)==2 and all(r['status']=='waiting_credentials' and r['attempts']==0 for r in rows)
 assert not m._notification_client.calls
 assert n.status(m)['counts']['waiting_credentials']==2 and not n.status(m)['configured']
 m._notification_client.configured=lambda:True
 await n.tick(m);await n.tick(m)
 assert all(r['status']=='sent' for r in m.db.list(n.KIND))
 assert len(m._notification_client.calls)==3


async def test_disabling_notification_cancels_credential_wait(env):
 m,task,c,p,record=env;m._notification_client.configured=lambda:False
 await n.tick(m)
 task['config']['notifications']['enabled']=False;m.db.put('task',task['id'],task)
 await n.tick(m)
 assert all(r['status']=='cancelled' for r in m.db.list(n.KIND))


def test_switching_app_reuploads_unsent_file_with_new_idempotency_key(env,monkeypatch):
 m,task,c,p,record=env
 monkeypatch.setenv('LARK_APP_ID','cli_old');n.enqueue(m)
 row=next(r for r in m.db.list(n.KIND) if r['kind']=='resume')
 row.update(status='retry',file_key='old-app-upload',attempts=1);n._save(m.db,row)
 monkeypatch.setenv('LARK_APP_ID','cli_new');n._process(m,m._notification_client,row)
 saved=m.db.get(n.KIND,row['id'])
 assert saved['status']=='sent' and saved['app_id']=='cli_new'
 assert m._notification_client.uploads==1
 assert m._notification_client.calls[0][3]['file_key']!='old-app-upload'
 assert m._notification_client.calls[0][4]==n._key(['cli_new',row['id'],'file'])[:32]
 assert m._notification_client.calls[0][4]!=n._key(['cli_old',row['id'],'file'])[:32]


@pytest.mark.parametrize('state,receipt,uncertain',[
 ('sending',None,False),('unknown',None,False),('retry','om_old',False),('cancelled',None,True),
])
def test_switching_app_never_replays_uncertain_or_partial_delivery(env,monkeypatch,state,receipt,uncertain):
 m,task,c,p,record=env
 monkeypatch.setenv('LARK_APP_ID','cli_old');n.enqueue(m)
 row=next(r for r in m.db.list(n.KIND) if r['kind']=='resume')
 row.update(status=state,file_key='old-app-upload',send_uncertain=uncertain)
 if receipt:row['file_message_id']=receipt
 n._save(m.db,row)
 monkeypatch.setenv('LARK_APP_ID','cli_new');n._process(m,m._notification_client,row)
 saved=m.db.get(n.KIND,row['id'])
 assert saved['status']=='failed' and saved['app_id']=='cli_old'
 assert saved['file_key']=='old-app-upload' and '应用已变化' in saved['error']
 # Reopening or inspecting the failed record must not make an uncertain send safe.
 n._process(m,m._notification_client,saved)
 assert not m._notification_client.calls and m._notification_client.uploads==0


async def test_switching_app_keeps_sent_events_deduplicated(env,monkeypatch):
 m,task,c,p,record=env
 monkeypatch.setenv('LARK_APP_ID','cli_old');await n.tick(m);await n.tick(m)
 monkeypatch.setenv('LARK_APP_ID','cli_new');await n.tick(m);await n.tick(m)
 assert len(m._notification_client.calls)==3
 assert all(r['status']=='sent' and r['app_id']=='cli_old' for r in m.db.list(n.KIND))


def test_legacy_waiting_queue_is_bound_only_when_preparing_to_send(env,monkeypatch):
 m,task,c,p,record=env;n.enqueue(m)
 row=next(r for r in m.db.list(n.KIND) if r['kind']=='resume')
 row.pop('app_id',None);row.update(status='waiting_credentials',file_key='legacy-file-key');n._save(m.db,row)
 monkeypatch.setenv('LARK_APP_ID','cli_new');n._process(m,m._notification_client,row)
 saved=m.db.get(n.KIND,row['id'])
 assert saved['status']=='sent' and saved['app_id']=='cli_new'
 assert m._notification_client.uploads==1


def test_app_changed_during_upload_stops_before_any_message(env,monkeypatch):
 m,task,c,p,record=env
 monkeypatch.setenv('LARK_APP_ID','cli_old');n.enqueue(m)
 row=next(r for r in m.db.list(n.KIND) if r['kind']=='resume')
 def upload(record,data):
  monkeypatch.setenv('LARK_APP_ID','cli_new');return 'old-app-upload'
 m._notification_client.upload=upload;n._process(m,m._notification_client,row)
 saved=m.db.get(n.KIND,row['id'])
 assert saved['status']=='failed' and saved['app_id']=='cli_old'
 assert not m._notification_client.calls


async def test_recovery_todo_push_is_task_scoped_once_per_incident(env):
 from cloakup.attention import AttentionStore
 m,task,c,p,record=env
 task['config']['notifications']['resume_chat_id']='';m.db.put('task',task['id'],task)
 existing=m.db.get('attention','todo1');existing['status']='resolved';m.db.put('attention','todo1',existing)
 other=copy.deepcopy(task);other['id']='other';other['config']['notifications']['todo_user_id']='ou_other';m.db.put('task','other',other)
 state={'task_id':'task1','incident_id':'first','status':'blocked','attempts':3,'max_attempts':3,'failures':['private-chat']}
 run={'id':'runner','status':'running','account_runner':True,'selection_recovery':{c['id']:state}}
 m.jobs[run['id']]=run;store=AttentionStore(m.db)
 store.reconcile([run],[c]);await n.tick(m);await n.tick(m)
 assert len(m._notification_client.calls)==1 and m._notification_client.calls[0][0]=='ou_admin'
 body=m._notification_client.calls[0][3]['text']
 assert '恢复尝试：3/3' in body and '打开任务执行浏览器' in body and 'private-chat' not in body
 state['status']='recovered';store.reconcile([run],[c]);await n.tick(m)
 state.update(status='blocked',incident_id='second');store.reconcile([run],[c]);await n.tick(m)
 assert len(m._notification_client.calls)==2
 assert m._notification_client.calls[0][4]!=m._notification_client.calls[1][4]


async def test_resolved_recovery_todo_cancels_notification_before_send(env):
 from cloakup.attention import AttentionStore
 m,task,c,p,record=env
 task['config']['notifications']['resume_chat_id']='';m.db.put('task',task['id'],task)
 existing=m.db.get('attention','todo1');existing['status']='resolved';m.db.put('attention','todo1',existing)
 state={'task_id':'task1','status':'blocked','attempts':3}
 run={'id':'runner','status':'running','selection_recovery':{c['id']:state}}
 store=AttentionStore(m.db);store.reconcile([run],[c]);n.enqueue(m)
 state['status']='recovered';store.reconcile([run],[c]);await n.tick(m)
 assert not m._notification_client.calls
 assert all(row['status']=='cancelled' for row in m.db.list(n.KIND))
