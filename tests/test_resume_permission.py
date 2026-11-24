from types import SimpleNamespace as NS
from cloakup.boss_resume import is_permission_text,reconcile_permission
from cloakup.autonomy import next_step
from cloakup.autopilot import revisit_question


def test_permission_buttons_are_not_candidate_decline_or_resume():
 from cloakup.boss_resume import is_resume_permission_text
 p={'inbox':[{'external_id':'card','text':'对方想发送附件简历给您，您是否同意\n拒绝\n同意'}],'outbox':[], 'resumes':[], 'contacts':[], 'status':'contacted'}
 assert is_permission_text(p['inbox'][0]['text'])
 assert is_resume_permission_text(p['inbox'][0]['text'])
 assert is_resume_permission_text('对方想发送附件简历给您，您是否同意 拒绝 同意')
 assert not is_resume_permission_text('我想要和您交换微信，您是否同意')
 assert next_step(p)['action']=='wait'
 assert not is_permission_text('我不同意发送简历')


def test_acceptance_does_not_claim_file_received():
 events=[];store=NS(audit=lambda *a,**kw:events.append(a[1]))
 action={'id':'a','kind':'accept_resume','status':'awaiting_verification','before_message_ids':['old'],'accepted_before':False}
 p={'id':'p','platform_actions':[action],'resumes':[]}
 reconcile_permission(store,{},p,{'messages':[]},{'requests':[],'accepted':[{'id':'ok'}]})
 assert action['status']=='accepted' and not p['resumes']
 assert events==['resume_permission_verified']


def test_request_disappearing_alone_does_not_prove_acceptance():
 store=NS(audit=lambda *a,**kw:None)
 action={'id':'a','kind':'accept_resume','status':'claimed','before_message_ids':[]}
 p={'id':'p','platform_actions':[action]}
 reconcile_permission(store,{},p,{'messages':[]},{'requests':[],'accepted':[]})
 assert action['status']=='claimed'


def test_later_manual_answer_prevents_reopening(monkeypatch):
 from cloakup import autopilot
 monkeypatch.setattr(autopilot,'can_revisit_question',lambda *a:True)
 p={'id':'p','pending_question_id':'q','handled_reply_id':'q','outbox':[{'status':'sent','decision':{'reply_id':'q'},'receipt':{'message_id':'old'}}]}
 store=NS(save=lambda *a:None,audit=lambda *a,**kw:None)
 revisit_question(store,{},p,{'messages':[{'direction':'out','external_id':'manual'}]})
 assert 'pending_question_id' not in p and p['handled_reply_id']=='q'


def test_existing_history_can_gain_verified_resume_flag(tmp_path):
 from cloakup.sourcing import CaseStore
 from cloakup.autopilot import sync_snapshot
 store=CaseStore(tmp_path);case=store.create('https://www.zhipin.com/web/chat/job/edit?encryptId=x')
 person=store.add_candidate(case,'p','https://www.zhipin.com/web/chat/index',{'summary':'职业资料'})
 person['conversation']={'chat_id':'12-0','receiver':'甲','job_title':'岗位'}
 snap={'valid':True,'chat_id':'12-0','fingerprint':'f','coverage':'已加载','messages':[{'external_id':'file','direction':'in','text':'职业简历.pdf\n点击预览附件简历','attachment':True}]}
 sync_snapshot(store,case,person,snap)
 assert not person['resumes']
 snap['messages'][0]['resume']=True
 sync_snapshot(store,case,person,snap)
 sync_snapshot(store,case,person,snap)
 assert person['resumes']==['platform-message:file']
