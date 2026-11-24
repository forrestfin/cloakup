from cloakup.resume_closure import pending,MESSAGE,summary

def test_permission_is_not_received_and_ack_never_repeats():
 p={'resumes':[],'outbox':[],'platform_actions':[{'kind':'accept_resume','status':'accepted'}]}
 assert pending(p) is None
 p['resumes']=['platform-message:file'];batch=pending(p);assert batch
 p['outbox']=[{'status':'sent','text':MESSAGE,'decision':{'resume_receipt_ack':True,'resume_receipt_refs':batch['refs'],'resume_receipt_hashes':[]}}]
 assert pending(p) is None and summary(p)['status']=='sent'
 p['outbox'][0]['status']='delivery_unknown';assert pending(p) is None and summary(p)['status']=='delivery_unknown'
 p['resumes'].append('platform-message:new');assert pending(p)['refs']==['platform-message:new']

def test_same_file_hash_with_new_platform_reference_is_not_thanked_twice():
 p={'resumes':['platform-message:old','platform-message:new'],'resume_files':[{'source_ref':'platform-message:new','sha256':'same-file'}],'outbox':[{'status':'sent','decision':{'resume_receipt_ack':True,'resume_receipt_refs':['platform-message:old'],'resume_receipt_hashes':['same-file']}}]}
 assert pending(p) is None

def test_existing_explicit_thanks_is_preserved_without_resending():
 p={'resumes':['platform-message:file'],'history':[{'external_id':'file','direction':'in','resume':True,'observed_at':100}],'outbox':[{'status':'sent','sent_at':110,'text':'简历已收到，谢谢您！'}]}
 assert pending(p) is None
 p['history'][0].pop('resume');assert pending(p) is None
 p['outbox'][0]['sent_at']=90;assert pending(p) is not None


def test_closure_addresses_last_invitation_instead_of_repeating_file_receipt():
 from cloakup.resume_closure import render
 p={'resumes':['file'],'outbox':[]}
 snap={'messages':[{'direction':'in','external_id':'file','resume':True,'text':'简历.pdf'},{'direction':'in','external_id':'reply','text':'嗯嗯，有需要可以直接电联高效沟通哈'}]}
 r=render({},p,snap)
 assert r['context']['kind']=='phone_invitation' and r['context']['latest_reply_id']=='reply'
 assert '需要沟通时' in r['text'] and '简历已收到' not in r['text']
 assert '马上打电话' not in r['text']
 # Raw identities and actual numbers are not copied into the reply.
 snap['messages'][-1]['text']='可以直接电话联系13812345678，找某某'
 assert '13812345678' not in render({},p,snap)['text']


def test_questions_and_later_are_not_closed_with_thanks():
 from cloakup.resume_closure import render
 p={'resumes':['file'],'outbox':[]}
 for text in ['晚点再聊，我在忙','请问工作地点在哪里？','底薪是多少？']:
  assert render({},p,{'messages':[{'external_id':'q','direction':'in','text':text}]}) is None


def test_file_only_fallback_and_actual_reply_have_different_messages():
 from cloakup.resume_closure import render
 case={'execution':{'resume_receipt_message':'简历收到了，谢谢。'}};p={'outbox':[]}
 snap={'messages':[{'external_id':'f','direction':'in','text':'简历.pdf','attachment':True,'resume':True}]}
 assert render(case,p,snap)['text']=='简历收到了，谢谢。'
 snap['messages'].append({'external_id':'r','direction':'in','text':'谢谢您，辛苦啦'})
 assert render(case,p,snap)['context']['kind']=='courtesy'
 assert render(case,p,snap)['text']!='简历收到了，谢谢。'
 snap['messages'].append({'external_id':'control','direction':'in','text':'我想要和您交换微信，您是否同意\n拒绝\n同意'})
 assert render(case,p,snap)['context']['latest_reply_id']=='r'
