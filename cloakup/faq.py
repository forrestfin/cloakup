"""Local, administrator-confirmed answers. Candidate text never leaves this module."""
import re
from .sourcing import SourcingError

QUESTION = re.compile(r'[?？]|请问|能否|是否|为什么|怎么|多少|什么|能不能|可以吗|想知道|想了解')
TOPICS = {'compensation':r'薪|工资|待遇|提成|奖金|底薪|收入',
          'cooperation':r'合同|用工|全职|兼职|合作|结算|社保|佣金|分成',
          'remote':r'远程|地点|在哪|地址|坐班|办公|出差',
          'job_details':r'岗位|职位|工作内容|职责|要求'}

def question_topic(text):
    for name,pattern in TOPICS.items():
        if re.search(pattern,text):return name
    return 'other'

def needs_confirmation(text):
    return bool(QUESTION.search(text) or re.search(TOPICS['compensation']+'|'+TOPICS['cooperation'],text))

def match_faq(case,text):
    # An exact full question is deliberately conservative: keyword-only matching
    # can answer just one clause of a multi-part question and hide the remainder.
    normalize=lambda t:re.sub(r'[\s?？!！。]+','',t).casefold()
    matches=[f for f in case.get('faq',[]) if f.get('enabled') and
             any(normalize(text)==normalize(q) for q in [f['question'],*f.get('variants',[])])]
    return matches[0] if len(matches)==1 else None

def checked_faq(items):
    from .autonomy import validate_reply
    if not isinstance(items,list) or len(items)>100:raise SourcingError('常见问题最多100条')
    seen=set()
    for f in items:
        if not isinstance(f,dict) or set(f)!={'question','answer','variants','enabled'}:raise SourcingError('常见问题格式不合法')
        if type(f['enabled']) is not bool or not isinstance(f['question'],str) or not 1<=len(f['question'].strip())<=200:raise SourcingError('请填写常见问题与启用状态')
        if not isinstance(f['variants'],list) or len(f['variants'])>20 or any(not isinstance(q,str) or not 1<=len(q.strip())<=200 for q in f['variants']):raise SourcingError('相同问法最多20条，每条1至200字')
        validate_reply(f['answer'],[],[])
        for q in [f['question'],*f['variants']]:
            key=re.sub(r'[\s?？!！。]+','',q).casefold()
            if not key or key in seen:raise SourcingError('常见问题或相同问法重复')
            seen.add(key)
    return items
