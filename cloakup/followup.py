"""Bounded re-engagement. Receipt is evidence; elapsed time never proves a read."""
import hashlib,time,json
from datetime import datetime
from zoneinfo import ZoneInfo

DEFAULTS={'nudge_enabled':True,'nudge_unread_hours':168,'nudge_read_hours':72,'nudge_max_attempts':2,'nudge_start_hour':9,'nudge_end_hour':20}

# 对话体验底线（2026-11-24 起）：对方没回复时连续发消息会让人不适。配置只能比这些更严，不能放宽。
NO_REPLY_MAX_ATTEMPTS=1   # 从未回复过的人：首聊之后最多再追 1 次
REPLIED_MAX_ATTEMPTS=2    # 回复过、后来沉默的人：最多追 2 次
MIN_READ_HOURS=72         # 已读未回：至少隔 3 天
QUIET_START,QUIET_END=9,20  # 主动追聊只在上海时间 9–20 点发；回复对方来信不受此限
# 上一条只是“送达”、对方还没看：不追聊；超过 nudge_unread_hours（默认 7 天）仍未读就结束追聊。


def limits(cfg):
 """把任务配置收敛到体验底线以内，供保存配置与运行时共用。"""
 return {**cfg,'nudge_read_hours':max(MIN_READ_HOURS,cfg.get('nudge_read_hours',DEFAULTS['nudge_read_hours'])),
         'nudge_max_attempts':min(REPLIED_MAX_ATTEMPTS,cfg.get('nudge_max_attempts',DEFAULTS['nudge_max_attempts'])),
         'nudge_start_hour':max(QUIET_START,cfg.get('nudge_start_hour',QUIET_START)),
         'nudge_end_hour':min(QUIET_END,cfg.get('nudge_end_hour',QUIET_END))}

def correction_key(answer):
 return hashlib.sha256(json.dumps({'source':answer.get('source'),'sources':answer.get('sources',[])},sort_keys=True,ensure_ascii=False).encode()).hexdigest()

def positive_resume_at(person,incoming,last):
 """候选人有正向回复、我们已回复但仍未索要或收到简历时，返回约两小时后的主动索要时刻；否则返回 None。

 每条正向回复最多主动索要一次；已有任何简历请求（文字或平台按钮）或已用“简历”措辞回复过则不再重复。
 时刻按候选人与该回复稳定抖动到 1.7～2.6 小时，避免整点发送。
 """
 from .autonomy import intent_labels
 from .pacing import stable_fraction
 if not incoming or not incoming.get('observed_at') or 'interest' not in intent_labels(incoming.get('text','')):return None
 at=incoming['observed_at']
 if last.get('sent_at',0)<at:return None
 if any(a.get('kind')=='ask_resume' and a.get('at',0)>=at for a in person.get('platform_actions',[])):return None
 if any((m.get('sent_at') or m.get('claimed_at') or 0)>=at and '简历' in (m.get('text') or '') and m['status'] in ('sending','sent','delivery_unknown') for m in person.get('outbox',[])):return None
 if any(m.get('decision',{}).get('nudge_anchor')==incoming['external_id'] for m in person.get('outbox',[])):return None
 return at+2*3600*(0.85+0.3*stable_fraction('positive_resume',person.get('id'),incoming['external_id']))


def evaluate(case,person,now=None):
 from .sourcing import TERMINAL
 from .autonomy import intent_labels,day_key
 from .boss_resume import is_permission_text
 now=time.time() if now is None else now;cfg=limits({**DEFAULTS,**case.get('execution',{})})
 incoming=next((m for m in reversed(person.get('inbox',[])) if not m.get('platform_control') and not is_permission_text(m.get('text',''))),None)
 anchor=incoming['external_id'] if incoming else 'initial'
 attempts=[m for m in person.get('outbox',[]) if m.get('decision',{}).get('nudge_anchor')==anchor and (m.get('claimed_at') or m['status'] in ('sending','sent','delivery_unknown'))]
 state={'anchor':anchor,'attempts':len(attempts),'max_attempts':min(NO_REPLY_MAX_ATTEMPTS if anchor=='initial' else REPLIED_MAX_ATTEMPTS,cfg['nudge_max_attempts']),'state':'waiting','due':False,'delivery':'unverified','variants':[]}
 if person.get('resumes') or person.get('handoff') or person.get('status') in TERMINAL:return {**state,'state':'closed'}
 if not cfg['nudge_enabled'] or not case.get('execution',{}).get('follow_up',True):return {**state,'state':'disabled'}
 if incoming and (set(intent_labels(incoming['text']))&{'decline','later'}):return {**state,'state':'paused_by_candidate'}
 if any(m['status'] in ('sending','delivery_unknown') for m in person.get('outbox',[])):return {**state,'state':'receipt_unknown'}
 sent=[m for m in person.get('outbox',[]) if m['status']=='sent' and m.get('approval_actor')=='automation' and m.get('sent_at')]
 if not sent:return {**state,'state':'no_verified_send'}
 last=max(sent,key=lambda m:m['sent_at'])
 if incoming and incoming.get('observed_at',0)>last['sent_at']:return {**state,'state':'reply_waiting'}
 # A later manual reply stays in control; do not chase over the consultant.
 history=[m for m in person.get('history',[]) if m.get('direction')=='out' and not m.get('platform_control')]
 if history and history[-1].get('external_id')!=last.get('receipt',{}).get('message_id'):return {**state,'state':'manual_or_unmatched_outbound'}
 delivery=last.get('receipt',{}).get('delivery_status','unverified');state['delivery']=delivery
 # 候选人已有正向回复、我们已回复但仍未索要简历：不等 24/48 小时的常规追聊，约两小时后主动文字索要一次。
 positive_at=positive_resume_at(person,incoming,last)
 if positive_at is None:
  if delivery not in ('read','delivered'):return {**state,'state':'receipt_unverified'}
  if delivery=='delivered':
   # 对方还没看到上一条：再发只会在对方列表里堆消息。不追聊；长期未读就结束。
   if now>=last['sent_at']+cfg['nudge_unread_hours']*3600:return {**state,'state':'exhausted','why':'unread_expired'}
   return {**state,'state':'waiting_read'}
  interval=cfg['nudge_read_hours']*3600
  # 追聊时刻在配置时长的 1.0～1.3 倍间按“候选人+上一条消息”稳定取值：不早于配置时长，每次重算一致。
  from .pacing import stable_fraction
  interval*=1.0+0.3*stable_fraction('nudge',person.get('id'),last.get('id'),anchor)
  base=max(last['sent_at'],last.get('receipt',{}).get('read_at',0))
  due_at=base+interval;state['due_at']=due_at
  if now<due_at:return state
  if len(attempts)>=state['max_attempts']:return {**state,'state':'exhausted'}
 else:
  state['due_at']=positive_at
  if now<positive_at:return {**state,'state':'positive_waiting'}
 schedule=case.get('task_policy',{}).get('schedule')
 if schedule:
  from .task_policy import window_status
  if not window_status(schedule,now)['allowed']:return {**state,'state':'outside_hours'}
 hour=datetime.fromtimestamp(now,ZoneInfo('Asia/Shanghai')).hour
 if not cfg['nudge_start_hour']<=hour<cfg['nudge_end_hour']:return {**state,'state':'outside_hours'}
 today=sum(m.get('claimed_at') is not None and day_key(m['claimed_at'])==day_key(now) for m in person.get('outbox',[]))
 if today>=case.get('autonomy',{}).get('max_messages_per_candidate_day',6):return {**state,'state':'daily_limit'}
 variants=['resume_after_interest'] if positive_at is not None else ['remind_resume']
 # Review a finite local result, never forward the candidate's raw messages.
 if incoming and positive_at is None:
  from .answering import answer_question
  answer=answer_question(case,incoming['text'],person=person)
  fingerprint=correction_key(answer)
  prior=[m for m in sent if m.get('decision',{}).get('reply_id')==incoming['external_id'] and not m.get('decision',{}).get('nudge')]
  if answer['action']=='answer' and prior and prior[-1]['text']!=answer['text'] and not any(m.get('decision',{}).get('nudge_correction_hash')==fingerprint for m in attempts):variants.append('clarify_previous')
 return {**state,'state':'due','due':True,'variants':variants}

def render(case,person,variant,state):
 if variant not in state['variants']:raise ValueError('追聊动作不在当前允许范围')
 if variant=='clarify_previous':
  from .answering import answer_question
  incoming=next(m for m in reversed(person['inbox']) if m['external_id']==state['anchor'])
  answer=answer_question(case,incoming['text'],person=person)
  # 隔了几天再更正，先点明是在补充回答对方之前的问题。
  return '补充回复您之前的问题：'+answer['text'],correction_key(answer)
 # 不再轮换“联系方式可以之后再交流”这类话术：对方没问过联系方式时读起来不对题。
 if state['anchor']=='initial':
  return '您好，想再确认一下您是否愿意了解这个机会；有兴趣的话方便时发份简历即可，暂时不考虑也没关系。',None
 texts=['您好，方便时发一份最新版简历吗？我先了解您的经历，再和您进一步沟通。',
        '最后跟进一次，若您仍有兴趣，方便时发份简历即可；暂时不考虑也没关系，后续我就不再主动打扰。']
 return texts[min(state['attempts'],1)],None
