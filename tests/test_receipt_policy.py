import pytest
from cloakup.receipt_policy import continuation,recovery_status

pytestmark=pytest.mark.usefixtures('no_jitter')

def fixture():
 p={'conversation':{'chat_id':'12-0'},'outbox':[{'status':'delivery_unknown','pre_send_history_ids':['old']}]}
 s={'valid':True,'chat_id':'12-0','messages':[{'external_id':'old','direction':'in','text':'简历已发'},{'external_id':'new','direction':'in','text':'嗯嗯，有需要可以直接电联高效沟通哈'}]}
 return p,s

def test_new_explicit_context_permits_only_distinct_phone_action():
 p,s=fixture();assert continuation(p,s)==['exchange_phone']
 assert p['outbox'][0]['status']=='delivery_unknown'
 s['messages'][-1]['text']='方便加微信联系';assert continuation(p,s)==['exchange_wechat']

def test_no_intent_no_anchor_no_permission_or_sending_never_unblocks():
 p,s=fixture();s['messages'][-1]['text']='谢谢';assert not continuation(p,s)
 p,s=fixture();s['messages'][-1]['text']='不要再打电话';assert not continuation(p,s)
 p,s=fixture();p['outbox'][0]['pre_send_history_ids']=['missing'];assert not continuation(p,s)
 p,s=fixture();s['chat_id']='wrong';assert not continuation(p,s)
 p,s=fixture();p['outbox'].append({'status':'sending'});assert not continuation(p,s)
 p,s=fixture();p['outbox'][0]['pre_send_history_ids'].append('new');assert not continuation(p,s)

def test_background_receipt_reads_never_escalate_by_elapsed_time():
 m={'claimed_at':100,'verification':{'checks':2}}
 assert recovery_status(m,200)['requires_admin'] is False
 r=recovery_status(m,1000);assert not r['requires_admin'] and r['checks']==3 and r['next_check_at']==1300
 assert not recovery_status(m,10000000)['requires_admin']
 assert m=={'claimed_at':100,'verification':{'checks':2}}

def test_capabilities_allow_contact_but_never_unknown_text_send():
 from cloakup.decision_runtime import capabilities
 p,s=fixture();p.update(id='p',status='resume_received',inbox=[{'external_id':'new','text':s['messages'][-1]['text']}],resumes=[],contacts=[])
 controls={'buttons':{'exchange_phone':{'enabled':True},'exchange_wechat':{'enabled':True}}}
 actions,_,_=capabilities({'execution':{'request_contacts':True},'autonomy':{}},p,s,controls)
 assert set(actions)=={'wait','exchange_phone'}
