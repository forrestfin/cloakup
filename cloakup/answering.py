"""Shared local answer router for sending, operator preview and to-do triage.

Only administrator-confirmed FAQ/anonymous job facts are answer sources. Raw
candidate text is used locally; no arbitrary question is disclosed to a model.
"""
import re
from .faq import match_faq

ANSWER_POLICY_VERSION = 6
ANSWER_ORDER = ['尊重拒绝、稍后与身份询问等对话边界','优先使用管理员启用的岗位常见问题','检索同岗位、已确认且类型对应的事实；保留来源与版本','完整事实直接回答；缺失或冲突直接生成待办，无需模型猜测','回答后按已有资料推进，避免重复索取']
TOPICS = {
 'location':(r'工作地(?:点|方)|办公地(?:点|方)|工作位置|在哪|哪里|哪儿|地点|地址', r'远程|地区|地点|办公地址|工作地点'),
 'remote':(r'远程|居家|坐班',r'远程|居家|坐班'),
 # 事实匹配只用通用句式（任意领域的“经验 N 年”），不内置岗位或行业词。
 'experience':(r'工作经验|从业经验|经验要求|几年|年限',r'(?:经验|经历).{0,8}(?:[0-9一二三四五六七八九十]+)\s*年|(?:[0-9一二三四五六七八九十]+)\s*年.{0,12}(?:经验|经历)'),
 'education':(r'学历|本科|大专|硕士',r'学历|本科|大专|硕士'),
 'requirements':(r'任职要求|岗位要求|有什么要求|什么要求|需要什么技能',r'经验|背景|学历|本科|要求|加分'),
 'duties':(r'工作内容|做什么|干什么|负责什么|岗位职责',r'负责|职责|工作内容'),
}
# These need an explicit FAQ, not inference from an unrelated work-arrangement fact.
SENSITIVE = re.compile(r'薪|工资|待遇|底薪|提成|奖金|佣金|合同|社保|结算|分成|出差|加班|双休|单休|上班时间|时间|周末|周一|保证|必须|一定')
UNCERTAIN = re.compile(r'待确认|待核实|待商议|需确认|需核实|尚未|不确定|未确定')
QUESTION_LIKE = re.compile(r'[?？]|请问|能否|是否|为什么|怎么|多少|什么|哪里|在哪|哪儿|能不能|几年|怎样|如何|吗[。！!\s]*$')
SHORT_QUESTION = re.compile(r'^(?:工作地点|办公地点|工作地址|岗位职责|工作内容|任职要求|学历要求|薪资待遇|待遇|薪资|工资|提成|合作方式)[呢？?\s]*$')


def answer_question(case,text,person=None):
    base={'policy_version':ANSWER_POLICY_VERSION,'config_revision':case.get('config_revision',0)}
    # A conversational invitation is not a request for an unknown job fact.
    # Match the whole utterance so a greeting cannot hide a real question.
    invitation=re.sub(r'[\s，,。！!？?：:]+','',text).lower()
    if re.fullmatch(r'(?:(?:boss|老板|您好|你好|您好呀|你好呀))*?(?:可以聊聊吗|可以聊一下吗|方便聊聊吗|方便沟通吗|能聊聊吗|可以了解一下吗|想了解一下|想聊聊|有兴趣了解|可以聊聊)(?:呀|呢)?',invitation):
        return {**base,'action':'continue','source':'conversation','reason':'候选人表达交流意愿，结合对话和已确认岗位事实推进，不生成事实缺失待办','sources':[],'fact_indices':[]}
    if re.fullmatch(r'(?:你好|您好|在吗|hi|hello)[?？!！。\s]*',text.strip(),re.I):
        return {**base,'action':'continue','source':'conversation','reason':'问候按对话流程推进，不是岗位事实问题','sources':[],'fact_indices':[]}
    # A disclosure boundary is known policy, not a missing numeric fact.
    # 薪酬问题按已确认的沟通边界直接回答：问结构（“薪酬结构如何”“底薪、提成是如何的”）给出已确认结构；
    # 问数字（多少、比例）说明进一步详聊。11-15 候选人 17、22 问结构，被当成缺事实只回了“需顾问核实”。
    compensation=len(text)<=60 and bool(re.search(r'底薪|薪资|薪酬|工资|月薪|年薪|提成|奖金|待遇|收入',text)) and not bool(re.search(r'社保|公积金|地点|地址|远程|岗位|职位|经验|学历|上班|合同|合作|双休|单休|住宿|餐补|[，,;；]|以及|同时',text))
    if compensation:
        from .communication_policy import SALARY_POLICY,check_compensation_disclosure
        approved=[]
        for item in case.get('faq',[]):
            if not item.get('enabled') or not re.search(r'薪|工资|底薪|提成',item['question']):continue
            try:check_compensation_disclosure(item['answer'])
            except ValueError:continue
            if all(word in item['answer'] for word in ('固定底薪','绩效','提成')):approved.append(item)
        values={f['answer'] for f in approved}
        quantity=bool(re.search(r'多少|金额|数额|比例|几个点|百分之|具体数字|能给',text))
        sources=[{'text':SALARY_POLICY,'source':'global_communication_policy'}]
        statement=next(iter(values)).replace('具体安排可以进一步详聊。','').replace('具体可以详聊。','').strip('，。 ') if len(values)==1 else None
        if quantity:
            subject='具体底薪' if '底薪' in text else '具体提成比例' if re.search(r'提成|比例|几个点|百分之',text) else '具体薪酬数字'
            body=subject+'可以在交换联系方式后进一步详聊。'
            if statement:
                explained=person and any(m.get('status')=='sent' and all(word in m.get('text','') for word in ('固定底薪','绩效','提成')) for m in person.get('outbox',[]))
                body+=('整体方案合理、有竞争力。' if explained and all(word in statement for word in ('合理','竞争力')) else statement+'。');sources.append({'question':approved[0]['question'],'text':approved[0]['answer']})
            return {**base,'action':'answer','source':'compensation_policy','text':body,'reason':'具体薪酬数字按已确认沟通边界回应，不承诺核实后报价，不生成缺数字待办','sources':sources,'fact_indices':[]}
        if statement:
            sources.append({'question':approved[0]['question'],'text':approved[0]['answer']})
            return {**base,'action':'answer','source':'compensation_policy','text':statement+'，具体安排可以进一步详聊。','reason':'薪酬结构问题按管理员确认的结构直接回答，不转顾问核实','sources':sources,'fact_indices':[]}
    faq=match_faq(case,text)
    if faq:
        from .communication_policy import check_compensation_disclosure
        try:check_compensation_disclosure(faq['answer'])
        except ValueError:return {**base,'action':'defer','source':'communication_policy','reason':'当前沟通策略只允许薪酬结构性说明，旧FAQ含具体量化待遇，需管理员更新口径','sources':[],'fact_indices':[]}
        return {**base,'action':'answer','source':'faq','text':faq['answer'],'faq_question':faq['question'],
                'reason':'唯一匹配已启用的岗位常见问题','fact_indices':[],'sources':[{'question':faq['question'],'text':faq['answer']}]}
    if not QUESTION_LIKE.search(text) and not SHORT_QUESTION.fullmatch(text.strip()):
        return {**base,'action':'continue','source':'conversation','reason':'不是待回答的问题，按资料收集流程推进','sources':[],'fact_indices':[]}
    topics=[name for name,(q,_) in TOPICS.items() if re.search(q,text)]
    from .facts import facts_for
    typed=facts_for(case)
    kinds={'location':'work_arrangement','remote':'work_arrangement','experience':'experience','education':'education','requirements':'requirements','duties':'duties'}
    matches=[];covered=[];sources=[]
    for topic in topics:
        candidates=[f for f in typed if f['kind']==kinds[topic] and f['status']=='confirmed' and f.get('scope')=='job' and not UNCERTAIN.search(f['value'])]
        # Equal copies are not independent or contradictory facts.
        candidates=list({f['value']:f for f in candidates}.values())
        if len(candidates)==1:
            covered.append(topic);fact=candidates[0]
            if fact['id'] not in [f['id'] for f in sources]:
                sources.append({**fact,'text':fact['value'],'index':fact.get('anonymous_index')})
                matches.append(fact['value'])
    unknown_part=bool((re.search(r'以及|另外|还有|同时|和|及|与',text) and len(topics)==1) or SENSITIVE.search(text) or not topics or len(covered)!=len(topics) or len(re.findall(r'[?？]',text))>1)
    if matches and not unknown_part:
        body='已确认的岗位安排是：'+''.join(value.rstrip('。；;')+'。' for value in matches)
        from .autonomy import validate_reply
        try:validate_reply(body,matches,list(range(len(matches))))
        except Exception:
            return {**base,'action':'defer','source':'insufficient','reason':'引用事实未通过对外消息校验','sources':sources,'fact_indices':[f['index'] for f in sources if f['index'] is not None]}
        return {**base,'action':'answer','source':'job_facts','text':body,'reason':'问题主题有唯一已确认事实支持','topics':topics,'sources':sources,'fact_indices':[f['index'] for f in sources if f['index'] is not None]}
    return {**base,'action':'defer','source':'insufficient','reason':'问题缺少完整、唯一的已确认依据，或涉及需要单独确认的条件','topics':topics,'sources':sources,'fact_indices':[f['index'] for f in sources if f['index'] is not None]}
