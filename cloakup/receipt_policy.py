"""Delivery proof and permission to perform a distinct later action are separate."""
import re,time
VERSION=2
RECHECK_SECONDS=300

def continuation(person,snapshot):
    """Only explicit, newer candidate intent can admit a distinct contact request.

    No inferred sent receipt, no resend, and no general message authorization.
    Require an anchor in the pre-send history followed by a new incoming turn.
    """
    if not snapshot.get('valid') or snapshot.get('chat_id')!=person.get('conversation',{}).get('chat_id'):return []
    unknown=[m for m in person.get('outbox',[]) if m['status']=='delivery_unknown']
    if not unknown or any(m['status']=='sending' for m in person.get('outbox',[])):return []
    messages=snapshot.get('messages',[])
    conversational=[m for m in messages if m.get('direction') in ('in','out') and not m.get('platform_control')]
    if not conversational or conversational[-1]['direction']!='in':return []
    latest=conversational[-1];text=latest.get('text','')
    from .autonomy import intent_labels
    if set(intent_labels(text))&{'decline','later'} or re.search(r'不.{0,5}(?:电话|电联|微信)|别.{0,5}(?:电话|微信)',text):return []
    index=messages.index(latest)
    for message in unknown:
        before=message.get('pre_send_history_ids') or []
        anchors=[i for i,m in enumerate(messages) if m['external_id'] in before]
        if not anchors or latest['external_id'] in before or index<=max(anchors):return []
    actions=[]
    if re.search(r'(?:可以|方便|直接|欢迎|随时|需要).{0,12}(?:电联|电话联系|电话沟通|打电话)|(?:电话|手机号).{0,6}(?:给你|给您|交换)',text):actions.append('exchange_phone')
    if re.search(r'(?:可以|方便|直接|欢迎|随时).{0,10}(?:加|交换|联系).{0,5}微信|(?:加|交换)我.{0,4}微信',text):actions.append('exchange_wechat')
    if person.get('resumes') and any(m.get('resume') and m.get('direction')=='in' and 'platform-message:'+m['external_id'] in person['resumes'] for m in messages):
        if not any(m.get('decision',{}).get('resume_receipt_ack') or re.search(r'简历.{0,5}(?:已)?收到|收到.{0,5}简历',m.get('text','')) for m in unknown):actions.append('acknowledge_resume')
    return actions

def recovery_status(message,now=None):
    """Track background reads without inventing a delivery deadline.

    Delivered/read are platform evidence. Lack of either label is not a
    timeout and does not itself require an administrator or permit a resend.
    """
    now=time.time() if now is None else now
    since=message.get('claimed_at') or message.get('created_at') or now
    checks=message.get('verification',{}).get('checks',0)
    from .pacing import jitter
    return {'since':since,'elapsed_seconds':max(0,int(now-since)),'next_check_at':now+jitter(RECHECK_SECONDS),
            'checks':checks+1,'requires_admin':False,'policy_version':VERSION}
