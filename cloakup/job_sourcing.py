"""Job-owned business preferences, snapshotted with the JD in validated bundles."""
import copy
from .greeting_policy import render

KEYS={'greeting','outreach_terms','recommend_filters'}

def checked(value,case):
 from .boss_recommend import checked_filters
 if not isinstance(value,dict) or set(value)!=KEYS:raise ValueError('岗位寻访配置需包含首聊、职业关键词与推荐条件')
 result=copy.deepcopy(value)
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
  'recommend_filters':case.get('recommend_filters') or legacy.get('recommend_filters',[])},case)

def apply(case,value):
 profile=checked(value,case);case['job_sourcing']=profile
 case['desired_greeting']=render(profile['greeting'],case)
 case['recommend_filters']=copy.deepcopy(profile['recommend_filters'])
 case.setdefault('execution',{})['outreach_terms']=copy.deepcopy(profile['outreach_terms'])
 return case
