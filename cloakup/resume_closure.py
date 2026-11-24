"""A received file gets one courteous acknowledgement before handoff."""
import hashlib,json,re

MESSAGE='简历已收到，谢谢您！我会认真查看，并及时与您联系，沟通后续安排。'
VERSION=2

def render(case,person,snapshot):
 """Respond to the last real turn, not to the file event alone.

 Text and contact details stay local. Known conversational cues are rendered
 without another model call; unanswered questions remain with the fact router.
 """
 from .boss_resume import is_permission_text
 from .autonomy import intent_labels,validate_reply
 turns=[m for m in snapshot.get('messages',[]) if m.get('direction')=='in' and not m.get('platform_control') and not is_permission_text(m.get('text','')) and not m.get('resume') and not m.get('attachment')]
 latest=turns[-1] if turns else None;text=latest.get('text','') if latest else ''
 if set(intent_labels(text))&{'decline','later'}:return None
 from .conversation_semantics import current
 from .answering import answer_question
 semantic=current(case,person)
 route=(semantic['answer']['action'] if semantic else answer_question(case,text,person=person)['action']) if text else 'continue'
 if route in ('answer','defer') and not any(m.get('status')=='sent' and m.get('decision',{}).get('reply_id')==latest['external_id'] and not m.get('decision',{}).get('pending_ack') for m in person.get('outbox',[])):return None
 if person.get('pending_question_id'):return None
 if re.search(r'(?:可以|方便|直接|随时|需要|欢迎).{0,15}(?:电联|电话|打电话)',text):
  kind='phone_invitation';body='好的，谢谢您，后续需要沟通时我再与您联系。'
 elif re.search(r'(?:加|交换|联系).{0,5}微信|微信.{0,8}(?:联系|沟通)',text):
  kind='wechat_invitation';body='好的，谢谢您，后续我们再联系。'
 elif re.search(r'谢谢|感谢|辛苦|麻烦您',text):
  kind='courtesy';body='好的，感谢您的配合，后续有进展再与您联系。'
 elif text:
  kind='reply_after_resume';body='好的，谢谢您！我先看一下简历，后续再与您沟通。'
 else:
  kind='file_only';body=case.get('execution',{}).get('resume_receipt_message',MESSAGE)
 validate_reply(body,[],[])
 return {'text':body,'context':{'version':VERSION,'kind':kind,'latest_reply_id':latest['external_id'] if latest else None},'reason':'结合最后一条真实回复收尾，不机械复述收到简历，不声称已经查看或承诺联系时间'}

def pending(person):
 refs=list(dict.fromkeys(person.get('resumes',[])))
 if not refs:return None
 files=person.get('resume_files',[]);covered=set();hashes=set()
 for message in person.get('outbox',[]):
  if message['status'] not in ('sending','delivery_unknown','sent'):continue
  decision=message.get('decision',{})
  if decision.get('resume_receipt_ack'):
   covered.update(decision.get('resume_receipt_refs',[]));hashes.update(decision.get('resume_receipt_hashes',[]))
  elif message['status']=='sent' and re.search(r'简历.{0,5}(?:已)?收到|收到.{0,5}简历',message.get('text','')):
   # Existing explicit receipt acknowledgements should not be repeated.
   for ref in refs:
    proof=next((m for m in person.get('history',[]) if 'platform-message:'+m['external_id']==ref and m['direction']=='in'),None)
    if proof and proof.get('observed_at',0)<=message.get('sent_at',0):covered.add(ref)
 for f in files:
  if f['sha256'] in hashes:covered.add(f['source_ref'])
 missing=[ref for ref in refs if ref not in covered]
 if not missing:return None
 return {'key':hashlib.sha256(json.dumps(sorted(missing)).encode()).hexdigest(),'refs':missing,
         'hashes':list({f['sha256'] for f in files if f['source_ref'] in missing})}

def summary(person):
 messages=[m for m in person.get('outbox',[]) if m.get('decision',{}).get('resume_receipt_ack')]
 if messages:
  latest=messages[-1];return {'status':latest['status'],'text':latest['text'],'at':latest.get('sent_at') or latest.get('claimed_at')}
 return {'status':'pending' if pending(person) else 'already_acknowledged' if person.get('resumes') else 'no_resume'}
