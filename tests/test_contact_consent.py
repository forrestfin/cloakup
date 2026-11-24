from types import SimpleNamespace as NS
from cloakup.boss_contact_consent import available,reconcile,needs_sync
from cloakup.boss_resume import is_permission_text
from cloakup.receipt_policy import continuation


def test_contact_permission_after_handoff_is_not_business_question():
 p={'id':'p','status':'resume_received','resumes':['file'],'handoff':{'done':True}}
 controls={'contact_requests':[{'kind':'accept_wechat'}]}
 assert 'accept_wechat' in available({},p,controls)
 assert not available({'execution':{'accept_contact_requests':False}},p,controls)
 assert is_permission_text('我想要和您交换微信，您是否同意\n拒绝\n同意')
 p['platform_actions']=[{'kind':'accept_wechat','status':'awaiting_verification'}]
 assert not available({},p,controls)


def test_consent_receipt_does_not_invent_contact_value():
 events=[];store=NS(audit=lambda *a,**kw:events.append(a[1]))
 p={'id':'p','platform_actions':[{'kind':'accept_wechat','status':'claimed','accepted_before':[]}]}
 reconcile(store,{},p,{'contact_accepted':[{'text':'已同意交换微信'}]})
 assert p['platform_actions'][0]['status']=='accepted'
 assert not p.get('contact_records')
 p['contact_records']=[{'kind':'wechat','value':'local_only'}]
 reconcile(store,{},p,{})
 assert p['platform_actions'][0]['status']=='obtained'


def test_distinct_thanks_requires_received_cv_and_later_incoming_anchor():
 p={'conversation':{'chat_id':'12-0'},'resumes':['platform-message:cv'],'outbox':[{'status':'delivery_unknown','text':'方便发简历吗','pre_send_history_ids':['cv']}]}
 s={'valid':True,'chat_id':'12-0','messages':[{'external_id':'cv','direction':'in','resume':True,'text':'resume.pdf'},{'external_id':'later','direction':'in','text':'嗯嗯，有需要可以直接电联高效沟通哈'}]}
 assert 'acknowledge_resume' in continuation(p,s)
 assert p['outbox'][0]['status']=='delivery_unknown'
 p['outbox'][0]['text']='简历已收到，谢谢您'
 assert 'acknowledge_resume' not in continuation(p,s)
 p['outbox'][0]['text']='方便发简历吗';s['messages'].pop()
 assert not continuation(p,s)


def test_unknown_lock_only_allows_context_proven_distinct_closure(tmp_path):
 import pytest
 from cloakup.sourcing import CaseStore,SourcingError
 s=CaseStore(tmp_path);c=s.create('https://www.zhipin.com/web/chat/job/edit?encryptId=x')
 s.bind_job(c,{'title':'招聘顾问','requirements':'招聘经历'})
 p=s.add_candidate(c,'p','https://www.zhipin.com/web/chat/index',{'summary':'招聘经历'})
 p['conversation']={'chat_id':'12-0','receiver':'顾问','job_title':'招聘顾问'}
 s.set_autonomy(c,True,source='test')
 old=s.draft(c,p['id'],'follow_up','方便发简历吗');s.authorize_automation(c,p['id'],old['id'],decision={'intent':'reply'})
 s.claim_send(c,p['id'],old['id']);old.update(status='delivery_unknown',pre_send_history_ids=['cv']);p['status']='resume_received';p['resumes']=['platform-message:cv']
 ack=s.draft(c,p['id'],'follow_up','简历已收到，谢谢您！');s.authorize_automation(c,p['id'],ack['id'],decision={'resume_receipt_ack':True,'resume_receipt_key':'cv'})
 with pytest.raises(SourcingError):s.claim_send(c,p['id'],ack['id'])
 snap={'valid':True,'chat_id':'12-0','messages':[{'external_id':'cv','direction':'in','text':'resume.pdf','resume':True},{'external_id':'later','direction':'in','text':'好的，有需要可以联系'}]}
 s.claim_send(c,p['id'],ack['id'],observed_snapshot=snap)
 assert ack['status']=='sending' and old['status']=='delivery_unknown'
