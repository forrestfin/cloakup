"""Durable, reviewed planning feedback. No candidate text is sent implicitly."""
import asyncio,copy,time,uuid
from .recruiting_ai import checked_brief
from .faq import checked_faq

BLOCKING={'queued','running','failed'}

def reviewed_text(text):
    if not isinstance(text,str) or not 1<=len(text.strip())<=4000:raise ValueError('请填写1至4000字的脱敏规划内容')
    checked_brief({'job':{'role':'规划补充','requirements':[text]},'candidate':{k:'' for k in ('experience','availability','visible_evidence','coverage')}})
    return text.strip()

class PlanningDialogue:
    def __init__(self,manager):
        self.m=manager;self.db=manager.db;self.workers={}
        for r in self.db.list('planning_request'):
            if r['status'] in ('queued','running'):
                r.update(status='failed',error='服务重启中断了规划，请重试；不会自动发送候选人消息')
                self.db.put('planning_request',r['id'],r)

    def definition(self,key):
        d=copy.deepcopy(self.m.definitions.get(key))
        try:chat=self.db.get('planning_dialogue',key)
        except FileNotFoundError:chat={'id':key,'revision':0,'messages':[]}
        d['planning_context']={'revision':chat['revision'],'messages':[{'role':m['role'],'content':m['content']} for m in chat['messages']]}
        return d

    async def submit(self,key,body,todo=None):
        if any(r['id']==key for r in self.db.list('agent_release')):
            raise ValueError('该任务已启用草案发布，请在规划端 Agent 中补充事实并验证后发布')
        definition=self.m.definitions.get(key)
        if body.get('reviewed') is not True:raise ValueError('请先复核规划内容已脱敏，不含候选人身份、联系方式和具体任职公司')
        content=reviewed_text(body.get('message',''))
        if any(r['task_id']==key and r['status'] in BLOCKING for r in self.db.list('planning_request')):
            raise ValueError('该任务已有未完成的规划，请等待或重试原请求')
        if todo:
            if todo['kind']!='question' or todo['status']=='resolved':raise ValueError('该待办不能补充答案')
            if todo['case_id'] not in definition['config'].get('case_ids',[]):raise ValueError('待办不属于该任务')
            answer=reviewed_text(body.get('answer',''))
            # Original question stays local. Only the operator-written anonymous
            # question and confirmed answer enter the Harness planning context.
            content='管理员已确认的岗位问答：\n问题：'+content+'\n答案：'+answer
        else:answer=None
        context=self.definition(key)['planning_context']
        if sum(len(m['content']) for m in context['messages'])+len(content)>30000:raise ValueError('规划上下文已较长，请将已确认边界整理到任务提示词中')
        if len(context['messages'])>=60:raise ValueError('规划对话已达30轮，请整理任务提示词后开始新任务')
        if body.get('expected_revision') is not None and body['expected_revision']!=context['revision']:raise ValueError('规划对话已更新，请刷新后再提交')
        request={'id':uuid.uuid4().hex,'task_id':key,'case_ids':definition['config'].get('case_ids',[]),'status':'queued','at':time.time(),
                 'content':content,'answer':answer,'todo_id':todo['id'] if todo else None,'case_id':todo['case_id'] if todo else None,
                 'candidate_id':todo['candidate_id'] if todo else None,'reply_id':todo['reply_id'] if todo else None,
                 'question':todo['question'] if todo else None,'faq_saved':False}
        self.db.put('planning_request',request['id'],request)
        if todo:
            todo.update(status='in_progress',planning_request_id=request['id'])
            todo['history'].append({'at':time.time(),'action':'planning_submitted','note':'补充答案并请求Harness重规划'})
            self.db.put('attention',todo['id'],todo)
        self.workers[request['id']]=asyncio.create_task(self.run(request['id']))
        return request

    async def retry(self,ident):
        r=self.db.get('planning_request',ident)
        if r['status']!='failed':raise ValueError('仅失败的规划可以重试')
        r.update(status='queued',error=None);self.db.put('planning_request',ident,r)
        self.workers[ident]=asyncio.create_task(self.run(ident));return r

    async def run(self,ident):
        r=self.db.get('planning_request',ident)
        try:
            r.update(status='running',error=None);self.db.put('planning_request',ident,r)
            # Existing sends finish before configuration revision changes.
            for _ in range(60):
                if not any(m['status']=='sending' for cid in r['case_ids'] for p in self.m.cases.load(cid)['candidates'].values() for m in p['outbox']):break
                await asyncio.sleep(1)
            else:raise ValueError('现有消息尚未取得回执，请核验后重试规划')
            if r['todo_id'] and not r['faq_saved']:
                from .job_config import view_config,save_config
                case=self.m.cases.load(r['case_id']);cfg=view_config(case);revision=cfg.pop('revision')
                existing=next((f for f in cfg['faq'] if f['question']==r['question']),None)
                if existing:existing.update(answer=r['answer'],enabled=True)
                else:cfg['faq'].append({'question':r['question'],'answer':r['answer'],'variants':[],'enabled':True})
                checked_faq(cfg['faq'])
                save_config(self.m.cases,case,{'expected_revision':revision,'config':cfg})
                person=self.m.cases.candidate(case,r['candidate_id'])
                person['pending_question_id']=r['reply_id']
                self.m.cases.audit(case,'admin_answer_confirmed',candidate_id=person['id'],reply_id=r['reply_id'],request_id=ident,answer=r['answer'])
                r['faq_saved']=True;self.db.put('planning_request',ident,r)
            try:chat=self.db.get('planning_dialogue',r['task_id'])
            except FileNotFoundError:chat={'id':r['task_id'],'revision':0,'messages':[]}
            if not any(m.get('request_id')==ident and m['role']=='user' for m in chat['messages']):
                chat['messages'].append({'role':'user','content':r['content'],'request_id':ident,'at':time.time()});chat['revision']+=1
                self.db.put('planning_dialogue',chat['id'],chat,history=True)
            record=await self.m.generate_definition_plan(r['task_id'],conversation=True)
            chat=self.db.get('planning_dialogue',r['task_id'])
            chat['messages'].append({'role':'assistant','content':record['reply'],'request_id':ident,'plan_id':record['id'],'at':time.time()})
            # Assistant response is already represented by the saved plan; keep
            # signature stable until the next user turn increments the revision.
            self.db.put('planning_dialogue',chat['id'],chat,history=True)
            if r['todo_id']:
                case=self.m.cases.load(r['case_id']);person=self.m.cases.candidate(case,r['candidate_id'])
                person['planning_answer_ready']={'reply_id':r['reply_id'],'request_id':ident,'plan_id':record['id']}
                self.m.cases.audit(case,'answer_plan_ready',candidate_id=person['id'],plan_id=record['id'],reply_id=r['reply_id'])
            r.update(status='succeeded',plan_id=record['id'],finished_at=time.time());self.db.put('planning_request',ident,r)
        except BaseException as exc:
            r.update(status='failed',error='规划已中断，请重试' if isinstance(exc,asyncio.CancelledError) else str(exc)[:800])
            self.db.put('planning_request',ident,r)
            if isinstance(exc,asyncio.CancelledError):raise
        finally:self.workers.pop(ident,None)
