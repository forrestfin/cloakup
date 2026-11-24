"""Typed, scoped, versioned job facts. Legacy imports use explicit anchors only."""
import copy,re,uuid
from .recruiting_ai import checked_brief

KINDS={'work_arrangement':'工作安排','experience':'经验要求','education':'学历要求','requirements':'任职要求','duties':'工作职责','compensation':'薪酬','cooperation':'合作条款'}
# 锚点只用与岗位无关的通用句式，不内置任何岗位或行业的词；多个 JD 共用同一套规则。
YEARS=r'(?:[0-9一二三四五六七八九十]+)\s*年'
ANCHORS={
 'work_arrangement':r'远程|居家|地区不限|工作地点|办公地点|办公地址|工作地址|坐班',
 # “X经验N年”“N年以上X经验”，X 为任意领域。
 'experience':r'(?:经验|经历).{0,8}'+YEARS+r'|'+YEARS+r'.{0,12}(?:经验|经历)',
 'education':r'学历|本科|硕士|博士|大专',
 # “XX背景”“相关背景优先”等任意领域的背景要求。
 'requirements':r'任职要求|基本要求|岗位要求|[\u4e00-\u9fa5A-Za-z]{1,8}背景',
 'duties':r'负责.{1,40}|岗位职责|工作职责|主要职责',
}

def facts_for(case):
 if 'confirmed_facts' in case:return copy.deepcopy(case['confirmed_facts'])
 out=[]
 for i,value in enumerate((case.get('anonymous_job') or {}).get('requirements',[])):
  for kind,pattern in ANCHORS.items():
   if re.search(pattern,value):out.append({'id':f'legacy-{i}-{kind}','kind':kind,'value':value,'status':'confirmed','scope':'job','source':'legacy_reviewed_text','revision':case.get('config_revision',0),'anonymous_index':i})
 return out

def checked_facts(items,revision):
 if not isinstance(items,list) or len(items)>100:raise ValueError('岗位事实最多100条')
 out=[];ids=set()
 for item in items:
  if not isinstance(item,dict) or item.get('kind') not in KINDS or item.get('status') not in ('confirmed','unconfirmed'):raise ValueError('事实类型或确认状态不合法')
  value=item.get('value','').strip()
  if not 1<=len(value)<=500:raise ValueError('每条事实需要1至500字')
  checked_brief({'job':{'role':'岗位事实','requirements':[value]},'candidate':{k:'' for k in ('experience','availability','visible_evidence','coverage')}})
  ident=item.get('id') or uuid.uuid4().hex
  if ident in ids:raise ValueError('事实标识重复')
  ids.add(ident)
  out.append({'id':ident,'kind':item['kind'],'value':value,'status':item['status'],'scope':'job','source':'administrator','revision':revision})
 return out

def effective_job(case):
 job=copy.deepcopy(case.get('anonymous_job'))
 if not job:return None
 if 'confirmed_facts' in case:
  values=[f['value'] for f in case['confirmed_facts'] if f['status']=='confirmed' and f.get('scope')=='job']
  values += [v for v in job.get('requirements',[]) if not any(re.search(pattern,v) for pattern in ANCHORS.values())]
  job['requirements']=list(dict.fromkeys(values))
 return job
