from datetime import datetime
from zoneinfo import ZoneInfo
from cloakup.followup import evaluate,render

NOW=datetime(2026,11,16,10,tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
HOUR=3600
# 体验底线：未读不追；已读至少 72 小时（1.0～1.3 倍稳定取值，即 72～93.6 小时）；
# 从未回复最多追 1 次，回复过后沉默最多追 2 次；主动追聊只在 9–20 点。
def setup(status='read',age=100,pid='p',inbox=None):
 c={'execution':{'nudge_enabled':True},'autonomy':{'max_messages_per_candidate_day':6}}
 p={'id':pid,'status':'waiting_reply','inbox':inbox or [],'resumes':[],'outbox':[{'id':'initial','status':'sent','approval_actor':'automation','sent_at':NOW-age*HOUR,'claimed_at':NOW-age*HOUR,'text':'岗位问候','receipt':{'delivery_status':status,'message_id':'m'}}]}
 return c,p

def nudge(p,n,anchor='initial',age=50,status='read'):
 p['outbox'].append({'id':'n%d'%n,'status':'sent','approval_actor':'automation','sent_at':NOW-age*HOUR,'claimed_at':NOW-age*HOUR,'text':'追聊','decision':{'nudge':True,'nudge_anchor':anchor},'receipt':{'delivery_status':status}})

def test_unread_message_is_never_chased_and_expires_after_a_week():
 # 11-22 15:31 送达未读，11-23 18:27 又追了一条：对方还没看到上一条时不再追。
 c,p=setup(status='delivered',age=30);s=evaluate(c,p,NOW)
 assert s['state']=='waiting_read' and not s['due']
 c,p=setup(status='delivered',age=167);assert evaluate(c,p,NOW)['state']=='waiting_read'
 c,p=setup(status='delivered',age=169);s=evaluate(c,p,NOW);assert s['state']=='exhausted' and s['why']=='unread_expired'

def test_read_message_waits_at_least_three_days_even_if_config_says_less():
 c,p=setup(age=71);c['execution']['nudge_read_hours']=24
 assert not evaluate(c,p,NOW)['due']
 due=[evaluate(*setup(pid='p%d'%i,age=10),NOW)['due_at'] for i in range(20)]
 sent=NOW-10*HOUR
 assert all(sent+72*HOUR<=d<=sent+93.6*HOUR for d in due) and len(set(due))>10
 c,p=setup(age=94);assert evaluate(c,p,NOW)['due']
 p['outbox'][0]['receipt']['read_at']=NOW-HOUR;assert not evaluate(c,p,NOW)['due']

def test_no_reply_candidate_gets_at_most_one_follow_up():
 c,p=setup(age=300);c['execution']['nudge_max_attempts']=3
 assert evaluate(c,p,NOW)['max_attempts']==1
 nudge(p,0,age=200)
 s=evaluate(c,p,NOW);assert s['state']=='exhausted' and s['attempts']==1

def test_replied_candidate_gets_at_most_two_and_new_reply_resets_count():
 inbox=[{'external_id':'r1','text':'在的','observed_at':NOW-400*HOUR}]
 c,p=setup(age=300,inbox=inbox);c['execution']['nudge_max_attempts']=3
 s=evaluate(c,p,NOW);assert s['due'] and s['max_attempts']==2
 nudge(p,0,anchor='r1',age=250);nudge(p,1,anchor='r1',age=150)
 s=evaluate(c,p,NOW);assert s['state']=='exhausted' and s['attempts']==2
 p['inbox'].append({'external_id':'new','text':'感兴趣','observed_at':NOW})
 s=evaluate(c,p,NOW);assert s['attempts']==0 and s['state']=='reply_waiting'

def test_no_unknown_receipt_spam_or_night_nudge_even_if_config_allows_it():
 c,p=setup();p['outbox'][0]['status']='delivery_unknown';assert not evaluate(c,p,NOW)['due']
 c,p=setup();p['inbox']=[{'external_id':'r','text':'不考虑，请别联系'}];assert not evaluate(c,p,NOW)['due']
 # 11-18 23:08 的追聊：执行时段放到深夜、追聊时段被改成 0–24 点时也只在 9–20 点发。
 for hour in (23,8,20):
  c,p=setup();c['execution'].update(nudge_start_hour=0,nudge_end_hour=24)
  at=datetime(2026,11,16,hour,tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
  assert evaluate(c,p,at)['state']=='outside_hours'
 c,p=setup();p['resumes']=['file'];assert evaluate(c,p,NOW)['state']=='closed'

def test_follow_up_text_fits_the_conversation():
 c,p=setup();s=evaluate(c,p,NOW);text,_=render(c,p,'remind_resume',s)
 assert '简历' in text and '不考虑也没关系' in text and '联系方式' not in text
 inbox=[{'external_id':'r1','text':'在的','observed_at':NOW-400*HOUR}]
 c,p=setup(age=300,inbox=inbox);s=evaluate(c,p,NOW)
 text,_=render(c,p,'remind_resume',s);assert '最新版简历' in text
 s['attempts']=1;text,_=render(c,p,'remind_resume',s);assert '最后' in text and '不再主动打扰' in text

def test_saved_task_config_is_clamped_to_the_floor():
 from cloakup.followup import limits
 assert limits({'nudge_read_hours':24,'nudge_max_attempts':3,'nudge_start_hour':0,'nudge_end_hour':24}) == {
  'nudge_read_hours':72,'nudge_max_attempts':2,'nudge_start_hour':9,'nudge_end_hour':20}
 assert limits({'nudge_read_hours':120,'nudge_max_attempts':1,'nudge_start_hour':10,'nudge_end_hour':18})['nudge_read_hours']==120

def test_same_fact_correction_not_repeated_for_cosmetic_context_changes():
 from cloakup.followup import correction_key
 a={'source':'compensation_policy','sources':[{'text':'已确认薪酬结构'}],'text':'完整结构说明'}
 b={**a,'text':'已介绍过结构后的简短说明'}
 assert correction_key(a)==correction_key(b)
 assert correction_key(a)!=correction_key({**a,'sources':[{'text':'管理员更新后的结构'}]})


def test_old_wrong_compensation_reply_gets_one_prefixed_correction():
 # 11-15 候选人 22 问“底薪、提成是如何的？”，只回了“需顾问核实”；现在按已确认结构补充回答。
 faq={'question':'薪资福利是什么样的','answer':'薪酬采用固定底薪＋绩效工资＋提成的结构，整体方案合理、有竞争力，具体安排可以进一步详聊。','enabled':True,'variants':[]}
 c,p=setup(inbox=[{'external_id':'q','text':'底薪、提成是如何的？','observed_at':NOW-101*HOUR}])
 c['faq']=[faq]
 p['outbox'][0].update(text='谢谢回复。具体条件需要顾问进一步核实。',decision={'reply_id':'q'})
 s=evaluate(c,p,NOW);assert s['due'] and 'clarify_previous' in s['variants']
 text,key=render(c,p,'clarify_previous',s)
 assert text.startswith('补充回复您之前的问题：') and '固定底薪＋绩效工资＋提成' in text
 p['outbox'].append({'id':'fix','status':'sent','approval_actor':'automation','sent_at':NOW,'claimed_at':NOW,'text':text,
                     'decision':{'nudge':True,'nudge_anchor':'q','nudge_variant':'clarify_previous','nudge_correction_hash':key},'receipt':{'delivery_status':'read'}})
 assert 'clarify_previous' not in evaluate(c,p,NOW+100*HOUR)['variants']
