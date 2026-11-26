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

# 11-25 FDE交付工程师：绑定后脱敏岗位事实为空，加入任务失败。由本地按 JD 与寻访计划拼一版草稿（不再调用模型），
# 只填进表单供管理员核对；保存才算确认。只用通用句式与平台字段，不写死任何岗位或行业的词。
MONEY=re.compile(r'\d+(?:\.\d+)?\s*(?:[kKwW千万元]|/月|/年)')
PLACE=re.compile(r'[一-龥]{2,8}?(?:市|区|县)')
EXPERIENCE_FIELD=re.compile(r'\d+\s*-\s*\d+\s*年|\d+\s*年以[上内]|经验不限|应届')
EDUCATION_FIELD=re.compile(r'^(?:大专|本科|硕士|博士|学历不限)$')

def draft_anonymous_job(case):
 if case.get('anonymous_job'):return None
 snapshot=(case.get('job') or {}).get('snapshot') or {};plan=case.get('search_plan') or {}
 lines,dropped=[],0
 place=PLACE.match(str(snapshot.get('location') or '').strip())
 if place:lines.append('工作地点：'+place.group(0))
 fields=[str(v).strip() for v in snapshot.get('platform_fields') or []]
 lines+=['经验要求：'+v for v in fields if EXPERIENCE_FIELD.fullmatch(v)][:1]+['学历要求：'+v for v in fields if EDUCATION_FIELD.match(v)][:1]
 for item in plan.get('requirements') or []:
  text=str(item.get('requirement') or '').strip()
  if not text or item.get('kind') not in ('required','preferred'):continue
  lines.append(text if item['kind']=='required' else '加分项：'+text)
 from .recruiting_ai import IDENTIFIER
 kept=[]
 for line in lines:
  if IDENTIFIER.search(line) or MONEY.search(line) or len(line)>500:dropped+=1;continue
  if line not in kept:kept.append(line)
 if not kept:return None
 label=str((case.get('job') or {}).get('label') or '').strip()
 role=label if label and not IDENTIFIER.search(label) and len(label)<=40 else '当前岗位'
 kept=kept[:30]
 empty={k:'' for k in ('experience','availability','visible_evidence','coverage')}
 while kept:
  try:checked_brief({'job':{'role':role,'requirements':kept},'candidate':empty});break
  except ValueError:kept.pop();dropped+=1
 if not kept:return None
 facts=[{'id':uuid.uuid4().hex,'kind':kind,'value':line,'status':'confirmed'} for line in kept for kind,pattern in ANCHORS.items() if re.search(pattern,line)]
 return {'role':role,'requirements':kept,'facts':facts,'dropped':dropped}

# 11-26 16:04 qe工程师：候选人问汇报对象，JD 写明“直接向品质负责人汇报”却转了顾问。管理员定：JD 是岗位最主要的事实，原句可直接作答。
# 这里把 JD 拆成可对外的原句；薪酬数字、公司名、门牌、联系方式整句丢弃。只用通用句式，不写死任何岗位或行业的词。
JD_LIMIT=40
HEADINGS='岗位职责|工作职责|职位描述|岗位描述|工作内容|任职要求|任职资格|任职条件|岗位要求|职位要求|硬性条件|硬性要求|软性要求|加分项|我们提供|福利待遇|职位福利|薪酬福利|岗位亮点'
NUMBER=r'(?:(?<![A-Za-z\d])\d{1,2}(?:、|[.．](?!\d)|[)）])|（[一二三四五六七八九十]+）|(?<![一-龥])[一二三四五六七八九十]+、|[①-⑩])'
# 11-26 终审 I3／I4：除“1、”外还有“1.”“1)”“①”编号；小节标题可能粘在上一句末尾，在标题前也切开。
JD_SPLIT=re.compile(r'[\n。；;]+|\s*(?='+NUMBER+r')|\s*(?=(?:'+HEADINGS+r')(?:[\s:：]|\d|（|$))')
JD_NUMBER=re.compile(r'^\s*'+NUMBER+r'\s*')
JD_HEADING=re.compile(r'^(?:'+HEADINGS+r')[\s:：]*')
# 11-26 终审 C1：收入、待遇、期权等词与任何数字同句即丢弃，不依赖单位或“薪”字。
JD_PAY=re.compile(r'(?:收入|待遇|月入|年入|月均|薪|工资|底薪|提成|奖金|补贴|期权|股票|股权|分红|上浮|涨薪|调薪|打折|[一二三四五六七八九]折).*?(?:\d|[一二三四五六七八九十百千万])|(?:\d|[一二三四五六七八九十百千万]).*?(?:收入|待遇|月入|年入|薪|工资|底薪|提成|奖金|补贴|期权|股票|股权|分红|上浮|涨薪|调薪|[一二三四五六七八九]折)')
# 11-26 终审 I1／I2：座机、QQ、联系人，英文公司后缀，以及点名的客户／甲方。
JD_CONTACT=re.compile(r'电话|座机|手机|微信|QQ|邮箱|联系人|\d{3,4}[-\s]?\d{3,4}[-\s]?\d{3,4}|\d{6,}',re.I)
JD_ORG=re.compile(r'集团|股份|分公司|研究院|事务所|\b(?:Co\.|Ltd|Inc\.?|Corp|LLC|GmbH)\b|(?:客户|甲方|合作方|合作伙伴)(?:为|是|包括|有)',re.I)
# 11-26 只读演练：“5Why”被 MONEY 当成“5W”；JD 里的单位后面不能紧跟字母。
JD_MONEY=re.compile(r'\d+(?:\.\d+)?\s*(?:[kKwW](?![A-Za-z])|[千万元]|/月|/年)')
JD_ADDRESS=re.compile(r'[\dA-Za-z一二三四五六七八九十]+\s*(?:号楼|号|栋|座|楼|室|层)')

def _jd_reason(text,company):
 """返回整句不能对外的原因；能用返回 None。原因文字直接显示在“岗位与事实”页。"""
 from .communication_policy import check_compensation_disclosure
 from .recruiting_ai import IDENTIFIER
 from .autonomy import validate_reply
 if JD_MONEY.search(text) or JD_PAY.search(text):return '含薪酬数字'
 try:check_compensation_disclosure(text)
 except Exception:return '含薪酬数字'
 if JD_CONTACT.search(text):return '含联系方式'
 if JD_ORG.search(text) or (company and company in text):return '含公司或机构名'
 if IDENTIFIER.search(text):return '含公司或机构名' if '公司' in text else '含联系方式或身份标识'
 if JD_ADDRESS.search(text):return '含详细门牌'
 try:validate_reply(text,[],[])
 except Exception:return '含承诺用语或超长'
 return None

def jd_lines(case):
 """JD 逐句结果：[(句子, 原因)]，原因为 None 的句子可直接引用（末尾已补句号）。"""
 snapshot=(case.get('job') or {}).get('snapshot') or {}
 company=str(snapshot.get('company') or '').strip();out=[];seen=set();usable=0
 place=PLACE.match(str(snapshot.get('location') or '').strip())
 candidates=['工作地点：'+place.group(0)] if place else []
 for piece in JD_SPLIT.split(str(snapshot.get('requirements') or '')):
  text=piece.strip()
  while True:
   cut=JD_HEADING.sub('',JD_NUMBER.sub('',text)).strip()
   if cut==text:break
   text=cut
  candidates.append(text.rstrip('。；;，, '))
 for text in candidates:
  if len(text)<4:continue
  reason=_jd_reason(text,company)
  if reason is None:
   if not text.endswith(('！','？','!','?')):text+='。'
   if usable>=JD_LIMIT:reason='超过 %d 条上限'%JD_LIMIT
  if text in seen:continue
  seen.add(text);out.append((text,reason));usable+=reason is None
 return out

def jd_statements(case):
 return [text for text,reason in jd_lines(case) if reason is None]
