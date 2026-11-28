"""Job-owned business preferences, snapshotted with the JD in validated bundles."""
import copy,hashlib,json,time,uuid
from .greeting_policy import render

KEYS={'greeting','outreach_terms','recommend_filters','must_haves'}
MUST_HAVE_SOURCES={'jd','client_feedback'}

def checked_must_haves(items):
 if not isinstance(items,list):raise ValueError('岗位必须项需为列表')
 result,seen,ids=[],set(),set()
 for item in items:
  if not isinstance(item,dict) or not isinstance(item.get('text'),str):raise ValueError('每条岗位必须项需包含文字条件')
  text=item['text'].strip();note=item.get('note') or ''
  if not 1<=len(text)<=60:raise ValueError('每条岗位必须项为1至60字')
  if item.get('source') not in MUST_HAVE_SOURCES:raise ValueError('岗位必须项来源只能是岗位 JD 或客户反馈')
  if not isinstance(note,str) or len(note.strip())>120:raise ValueError('岗位必须项备注最多120字')
  if text in seen:continue
  ident=str(item.get('id') or 'mh_'+uuid.uuid4().hex[:8])
  if ident in ids:raise ValueError('岗位必须项标识重复')
  seen.add(text);ids.add(ident);added=item.get('added_at')
  result.append({'id':ident,'text':text,'source':item['source'],'note':note.strip(),'added_at':float(added) if isinstance(added,(int,float)) and not isinstance(added,bool) else time.time()})
 if len(result)>10:raise ValueError('每个岗位最多10条必须项')
 return result

def must_haves_revision(items):
 if not items:return ''
 return hashlib.sha256(json.dumps(sorted([str(v['id']),v['text']] for v in items),ensure_ascii=False).encode()).hexdigest()[:16]

def checked(value,case):
 from .boss_recommend import checked_filters
 if isinstance(value,dict) and 'must_haves' not in value:value={**value,'must_haves':[]} # 旧版本已发布配置无必须项
 if not isinstance(value,dict) or set(value)!=KEYS:raise ValueError('岗位寻访配置需包含首聊、职业关键词与推荐条件')
 result=copy.deepcopy(value)
 result['must_haves']=checked_must_haves(result['must_haves'])
 if not isinstance(result['greeting'],str):raise ValueError('岗位首聊必须为文字')
 render(result['greeting'],case)
 terms=result['outreach_terms']
 if not isinstance(terms,list) or len(terms)>30 or any(not isinstance(t,str) or not 1<=len(t.strip())<=40 for t in terms):raise ValueError('每个岗位最多30个职业关键词，每个1至40字')
 result['outreach_terms']=list(dict.fromkeys(t.strip() for t in terms))
 checked_filters(result['recommend_filters'])
 return result

def resolve(case,legacy=None):
 if case.get('job_sourcing') is not None:return checked(case['job_sourcing'],case)
 # Explicit compatibility projection only; new task saves never copy job A into B.
 legacy=legacy or {}
 return checked({'greeting':case.get('desired_greeting') or case.get('greeting_draft') or legacy.get('greeting') or case.get('platform_greeting_text',''),
  'outreach_terms':case.get('execution',{}).get('outreach_terms') or legacy.get('execution',{}).get('outreach_terms',[]),
  'recommend_filters':case.get('recommend_filters') or legacy.get('recommend_filters',[]),'must_haves':(case.get('job_sourcing') or {}).get('must_haves',[])},case)

def apply(case,value):
 profile=checked(value,case);case['job_sourcing']=profile
 case['desired_greeting']=render(profile['greeting'],case)
 case['recommend_filters']=copy.deepcopy(profile['recommend_filters'])
 case.setdefault('execution',{})['outreach_terms']=copy.deepcopy(profile['outreach_terms'])
 return case
