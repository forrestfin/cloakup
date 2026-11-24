from types import SimpleNamespace as NS
import pytest
from cloakup.boss_requests import available,reconcile,receipt_kind
from cloakup.storage import Database
from cloakup.action_gateway import ActionLedger

def person():return {'id':'p','status':'waiting_reply','inbox':[{'text':'你好'}],'resumes':[],'contacts':[],'platform_actions':[]}
def controls():return {'buttons':{k:{'enabled':True} for k in ('ask_resume','exchange_phone','exchange_wechat')},'cards':[]}
def test_available_uses_actual_buttons_and_blocks_duplicate_channels():
 p=person();c={'execution':{'request_contacts':True}};o=controls()
 assert len(available(c,p,o))==3
 p['platform_actions']=[{'kind':'exchange_phone','status':'pending'}]
 assert not available(c,p,o)
 p['platform_actions'][0]['status']='awaiting_verification'
 assert not available(c,p,o)
 p['platform_actions'][0]['status']='declined'
 assert not available(c,p,o)
 p['platform_actions'][0]['status']='obtained';p['contacts']=['13812345678']
 assert set(available(c,p,o))=={'ask_resume'}
 p['resumes']=['file'];assert not available(c,p,o)

def test_outbound_card_is_request_not_obtained_contact():
 p=person();events=[];s=NS(audit=lambda *a,**kw:events.append(a[1]))
 card={'id':'receipt','text':'请求交换电话 手机：13812345678','out':True,'incoming':False}
 reconcile(s,{},p,{'cards':[card]});reconcile(s,{},p,{'cards':[card]})
 assert len(p['platform_actions'])==1 and p['platform_actions'][0]['status']=='pending'
 assert not p['contacts'] and not available({'execution':{}},p,controls())

def test_incoming_contact_card_records_actual_values_and_resolves_unknown(tmp_path):
 db=Database(tmp_path);ledger=ActionLedger(db);p=person()
 ident=ledger.claim(task_id='c',resource='p',capability='exchange_phone',identity='once');ledger.transition(ident,'executing');ledger.transition(ident,'unknown')
 p['platform_actions']=[{'id':'a','kind':'exchange_phone','status':'awaiting_verification','operation_id':ident}]
 store=NS(db=db,audit=lambda *a,**kw:None)
 reconcile(store,{},p,{'cards':[{'id':'candidate-card','text':'手机号：13812345678','incoming':True,'out':False}]})
 assert p['contacts']==['13812345678'] and p['platform_actions'][0]['status']=='obtained'
 assert ledger.get(ident)['state']=='verified' and p['contact_records'][0]['receipt_id']=='candidate-card'
 reconcile(store,{},p,{'cards':[]});assert len(p['contacts'])==1

def test_actual_resume_and_request_are_separate():
 p=person();s=NS(audit=lambda *a,**kw:None)
 reconcile(s,{},p,{'cards':[{'id':'r','text':'希望您发送附件简历','out':True}]})
 assert p['platform_actions'][0]['kind']=='ask_resume' and not p['resumes']
 p['resumes']=['platform-message:file'];reconcile(s,{},p,{'cards':[]})
 assert p['platform_actions'][0]['status']=='obtained'

@pytest.mark.parametrize('kind,text', [('ask_resume','希望您发送附件简历'),('exchange_phone','请求交换电话'),('exchange_wechat','请求交换微信')])
async def test_click_once_receipt_and_duplicate_prevented(tmp_path,monkeypatch,kind,text):
 from cloakup.sourcing import CaseStore
 import cloakup.boss_requests as br
 store=CaseStore(tmp_path);c=store.create('https://www.zhipin.com/web/chat/job/edit?encryptId=j')
 store.bind_job(c,{'title':'岗位','requirements':'要求'});store.set_autonomy(c,True,source='test');c['execution']={'request_contacts':True}
 p=store.add_candidate(c,'person','https://www.zhipin.com/web/chat/index',{'summary':'招聘经历'})
 p['conversation']={'chat_id':'12-0','receiver':'甲','job_title':'岗位'};store.receive(c,p['id'],'hi','你好');store.save(c)
 calls=[]
 class Locator:
  async def count(self):return 1
  async def click(self,**kw):calls.append('click')
 class Session:
  page=NS(locator=lambda *a:Locator())
  async def _act(self,kind,fn):return await fn()
 async def checkpoint(*a):pass
 async def read(*a,**kw):return {'valid':True,'messages':[]}
 async def inspect(*a):
  o=controls();o['buttons'][kind]['selector']='#request'
  if calls:o['cards']=[{'id':'receipt','text':text,'out':True}]
  return o
 monkeypatch.setattr(br,'inspect',inspect);monkeypatch.setattr(br.boss_chat,'read_conversation',read)
 manager=NS(cases=store,checkpoint=checkpoint);job={'id':'run'}
 assert await br.execute(manager,job,Session(),c,p,kind)=='verified_request'
 saved=store.load(c['id']);p=store.candidate(saved,p['id'])
 assert p['platform_actions'][0]['status']=='pending' and not p.get('contacts')
 assert await br.execute(manager,job,Session(),saved,p,kind)=='not_available'
 assert calls==['click']
 assert await br.execute(manager,job,Session(),saved,p,'exchange_wechat')=='not_available'

async def test_harness_admits_native_buttons_after_answer_without_repeating_text(tmp_path):
 from cloakup import decision_runtime as dr
 p=person();p.update(outbox=[],conversation={'chat_id':'12-0'},handled_reply_id='reply',inbox=[{'external_id':'reply','text':'你好'}],online_resume_checked_at=1)
 c={'execution':{'request_contacts':True},'autonomy':{'max_messages_per_candidate_day':6}}
 actions,_,_=dr.capabilities(c,p,{},controls())
 assert {'ask_resume','exchange_phone','exchange_wechat'}<=set(actions)
 assert 'request_both' not in actions
 p['platform_actions']=[{'id':'a','kind':'exchange_phone','status':'pending','at':1}]
 actions,_,_=dr.capabilities(c,p,{},controls())
 assert set(actions)=={'wait'}

def test_resume_only_default_disables_optional_contact_requests():
 p=person();c={'execution':{}}
 assert set(available(c,p,controls()))=={'ask_resume'}
 p['resumes']=['actual-file']
 assert not available(c,p,controls())
