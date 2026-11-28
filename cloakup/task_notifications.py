"""Task-bound Feishu delivery. CVs go only to the configured group, never a model.

The durable queue records intent before network writes. A lost message response
is kept unknown; elapsed time and restarts never cause an ambiguous resend.
"""
import asyncio
import hashlib
import json
import os
import re
import time
import uuid
from urllib import request, error

DEFAULTS = {'enabled': False, 'resume_chat_id': '', 'todo_user_id': ''}
KIND = 'task_notification'


def validate_config(raw=None):
    if raw is None: raw = {}
    if not isinstance(raw, dict) or set(raw) - set(DEFAULTS):
        raise ValueError('飞书通知配置字段不合法')
    cfg = {**DEFAULTS, **raw}
    if type(cfg['enabled']) is not bool:
        raise ValueError('飞书通知开关不合法')
    for key, prefix in [('resume_chat_id', 'oc_'), ('todo_user_id', 'ou_')]:
        value = cfg[key]
        if not isinstance(value, str) or (value and not re.fullmatch(prefix+r'[A-Za-z0-9_-]{3,100}', value)):
            raise ValueError('简历群需填写 oc_ 群 ID，待办接收人需填写 ou_ 用户 Open ID')
    if cfg['enabled'] and not (cfg['resume_chat_id'] or cfg['todo_user_id']):
        raise ValueError('启用飞书通知前请至少设置一个接收目标')
    return cfg


class DeliveryError(Exception):
    def __init__(self, message, *, ambiguous=False, retryable=False):
        super().__init__(message)
        self.ambiguous, self.retryable = ambiguous, retryable


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # App secrets and bearer tokens must never follow a redirected request.
        return None


class FeishuClient:
    """Bot identity only; tokens remain in server memory, errors are sanitized."""
    def __init__(self):
        self.opener = request.build_opener(NoRedirect)
        self.token = None
        self.expires_at = 0
        self.credential_signature = None

    def configured(self):
        return bool(os.environ.get('LARK_APP_ID') and os.environ.get('LARK_APP_SECRET'))

    def _post(self, url, data, headers, *, message=False):
        req = request.Request(url, data=data, headers=headers, method='POST')
        try:
            with self.opener.open(req, timeout=25) as response:
                body = json.loads(response.read(1024*1024))
        except error.HTTPError as exc:
            # Do not persist a remote body or exception string: it can echo data.
            if exc.code == 429:
                raise DeliveryError('飞书请求限流，稍后重试', retryable=True) from None
            if exc.code in (401,403,404):
                if exc.code==401:self.token=None
                raise DeliveryError('飞书拒绝请求，请检查应用权限与接收目标（HTTP '+str(exc.code)+'）', retryable=True) from None
            if exc.code==400:
                try:body=json.loads(exc.read(1024*1024))
                except (OSError,ValueError):raise DeliveryError('飞书服务响应未确认',ambiguous=message,retryable=not message) from None
            else:
                raise DeliveryError('飞书服务响应未确认', ambiguous=message, retryable=not message) from None
        except (error.URLError, TimeoutError, OSError, ValueError):
            raise DeliveryError('飞书响应未确认，请检查网络', ambiguous=message, retryable=not message) from None
        if not isinstance(body, dict):
            raise DeliveryError('飞书返回格式未确认', ambiguous=message, retryable=not message)
        if body.get('code') != 0:
            code = body.get('code')
            auth_codes = {99991661, 99991663, 99991664, 99991665, 99991668}
            rejected_codes = auth_codes | {99991672, 99991400, 99991403, 230001, 230002, 230006, 230013}
            if code in auth_codes:self.token=None
            # Unknown service errors are not proof that a message was rejected.
            uncertain = message and code not in rejected_codes
            code = str(code) if type(code) is int else 'unknown'
            raise DeliveryError('飞书响应需核实（code '+code+'）' if uncertain else '飞书拒绝请求，请检查应用权限与接收目标（code '+code+'）', ambiguous=uncertain, retryable=not uncertain)
        return body

    def authenticate(self):
        if not self.configured(): raise DeliveryError('未配置飞书应用凭据', retryable=True)
        app_id, secret = os.environ['LARK_APP_ID'], os.environ['LARK_APP_SECRET']
        signature = hashlib.sha256((app_id+'\0'+secret).encode()).hexdigest()
        if self.token and self.expires_at > time.time()+120 and self.credential_signature == signature:
            return self.token
        body = self._post('https://accounts.feishu.cn/oauth/v3/token',
                          json.dumps({'grant_type':'client_credentials','client_id':app_id,'client_secret':secret}).encode(),
                          {'Content-Type':'application/json; charset=utf-8'})
        token, lifetime = body.get('access_token'), body.get('expires_in')
        if not isinstance(token, str) or not token or type(lifetime) is not int or lifetime <= 0:
            raise DeliveryError('飞书应用凭据未核验', retryable=True)
        self.token, self.expires_at, self.credential_signature = token, time.time()+lifetime, signature
        return token

    def upload(self, record, data):
        token = self.authenticate()
        boundary = 'cloakup'+uuid.uuid4().hex
        chunks = []
        for name, value in [('file_type', 'pdf' if record['extension']=='pdf' else 'stream'), ('file_name',record['filename'])]:
            chunks.append(('--'+boundary+'\r\nContent-Disposition: form-data; name="'+name+'"\r\n\r\n'+value+'\r\n').encode())
        # Fixed multipart filename prevents user filenames changing headers.
        chunks.append(('--'+boundary+'\r\nContent-Disposition: form-data; name="file"; filename="resume.'+record['extension']+'"\r\nContent-Type: application/octet-stream\r\n\r\n').encode())
        chunks.extend([data, ('\r\n--'+boundary+'--\r\n').encode()])
        body = self._post('https://open.feishu.cn/open-apis/im/v1/files', b''.join(chunks),
                          {'Authorization':'Bearer '+token,'Content-Type':'multipart/form-data; boundary='+boundary})
        key = body.get('data',{}).get('file_key')
        if not isinstance(key,str) or not key: raise DeliveryError('飞书文件上传未核验', retryable=True)
        return key

    def send(self, receiver, receiver_type, msg_type, content, stable_uuid):
        token = self.authenticate()
        body = self._post('https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type='+receiver_type,
                          json.dumps({'receive_id':receiver,'msg_type':msg_type,'content':json.dumps(content,ensure_ascii=False),'uuid':stable_uuid}).encode(),
                          {'Authorization':'Bearer '+token,'Content-Type':'application/json; charset=utf-8'}, message=True)
        ident = body.get('data',{}).get('message_id')
        if not isinstance(ident,str) or not ident:
            raise DeliveryError('飞书发送回执缺失，保留记录等待核实', ambiguous=True)
        return ident


def _key(parts):
    return hashlib.sha256(json.dumps(parts,ensure_ascii=False,sort_keys=True).encode()).hexdigest()


def _save(db, item):
    old = item.get('version',0)
    item['version'] = old+1
    item['updated_at'] = time.time()
    db.put(KIND,item['id'],item,expected=old)


def _bind_application(db, item):
    """Rebind unsent work only; a new bot cannot resume another bot's writes."""
    app_id = os.environ.get('LARK_APP_ID') or None
    if item.get('app_id') == app_id:
        return True
    if item['status'] == 'sent':
        return False
    has_receipt = any(item.get(key) for key in ('file_message_id','metadata_message_id','message_message_id'))
    uncertain = item['status'] in ('sending','unknown') or item.get('send_uncertain',False)
    if has_receipt or uncertain:
        item.update(status='failed',send_uncertain=uncertain,error='飞书应用已变化，原通知存在发送记录或结果未确认；保留原应用记录，未跨应用重发')
        _save(db,item)
        return False
    item['app_id'] = app_id
    item.pop('file_key',None)
    item.pop('sending_part',None)
    item.update(error=None,next_attempt_at=0)
    _save(db,item)
    return True


def _check_application(item):
    if item.get('app_id') != (os.environ.get('LARK_APP_ID') or None):
        raise DeliveryError('飞书应用在准备通知期间变化，已停止推送；请核验应用与接收目标')


def _owns_todo(manager, task, todo):
    ident = task['id']; cases = task['config'].get('case_ids',[])
    if todo.get('task_id'): return todo['task_id'] == ident
    if todo.get('case_id'): return todo['case_id'] in cases
    run = getattr(manager,'jobs',{}).get(todo.get('run_id'),{})
    if run.get('task_id'): return run['task_id'] == ident
    if run.get('account_runner'):
        return any(m['id']==ident and m['status']!='stopped' for m in manager.db.list('account_task'))
    return run.get('params',{}).get('case_id') in cases


# 11-27 10:43 系统暂停后，飞书待办与几十条问题、简历推送同一开头，没被注意到，停到 15:55。
# 系统暂停类待办单独标题；一直没处理时首次推送后约 30 分钟再提醒，之后约每 2 小时，每个待办每天最多 3 次，只在任务执行时段内。
REMIND_AFTER = (1800, 7200)
REMIND_DAILY_MAX = 3


def system_paused(manager, task_id):
    """任务被系统（不是管理员）暂停时返回暂停时刻，否则 None。"""
    member=next((m for m in manager.db.list('account_task') if m['id']==task_id),None)
    if not member or member.get('status')!='paused':return None
    last=(member.get('history') or [{}])[-1]
    if last.get('actor')=='admin':return None
    return last.get('at') or member.get('updated_at') or time.time()


def pause_alert(manager, task_id, todo):
    """该待办就是这次系统暂停的原因时返回暂停时刻：暂停类待办，且在暂停前 10 分钟内或之后生成。
    服务重启等其他系统暂停不能把早就开着、与这次暂停无关的待办标成“任务已暂停”。"""
    from .today import PAUSE_KINDS
    if todo.get('kind') not in PAUSE_KINDS:return None
    paused_at=system_paused(manager,task_id)
    if not paused_at or (todo.get('created_at') or 0)<paused_at-600:return None
    return paused_at


def _schedule(task):
    from .task_policy import DEFAULT_SCHEDULE
    return task['config'].get('schedule') or DEFAULT_SCHEDULE


def _in_window(task, now):
    from .task_policy import window_status
    return bool(window_status(_schedule(task),now)['allowed'])


def _reminder_due(manager, task, todo, chain, now):
    """chain：该待办已有的推送（首条 + 历次提醒），按序号排列。返回下一次是第几次提醒，或 None。"""
    if not chain or not pause_alert(manager,task['id'],todo) or not _in_window(task,now):return None
    live=[i for i in chain if i.get('status')!='cancelled']  # 任务恢复时取消的提醒不挡住之后的提醒
    last=live[-1] if live else None
    if not last or last.get('status')!='sent' or not last.get('sent_at'):return None
    from zoneinfo import ZoneInfo
    from datetime import datetime
    tz=ZoneInfo(_schedule(task).get('timezone','Asia/Shanghai'))
    today=datetime.fromtimestamp(now,tz).date()
    if sum(datetime.fromtimestamp(i.get('created_at',0),tz).date()==today for i in live)>=REMIND_DAILY_MAX:return None
    n=len(live)  # 第 n 次提醒；间隔按实际推送过的条数算，被取消的不算
    from .pacing import stable_fraction
    gap=REMIND_AFTER[min(n-1,len(REMIND_AFTER)-1)]*(1+0.2*stable_fraction(todo['id'],len(chain)))
    return n if now-last['sent_at']>=gap else None


def enqueue(manager):
    db=manager.db; existing={i['id']:i for i in db.list(KIND)}
    files=db.list('resume_file'); todos=db.list('attention')
    def add(task,kind,destination,source,**values):
        ident=_key([task['id'],kind,destination,source])
        if ident in existing:
            old=existing[ident]
            if old['status']=='cancelled':
                old.update(status='queued',error=None,next_attempt_at=0);_save(db,old)
            return
        item={'id':ident,'task_id':task['id'],'kind':kind,'destination':destination,'source':source,'app_id':os.environ.get('LARK_APP_ID') or None,
              'status':'queued','attempts':0,'created_at':time.time(),'version':0,**values}
        _save(db,item); existing[ident]=item
    for task in manager.definitions.list():
        cfg=validate_config(task['config'].get('notifications'))
        if not cfg['enabled']: continue
        if cfg['resume_chat_id']:
            for record in files:
                if record.get('status')=='archived' and record['case_id'] in task['config'].get('case_ids',[]):
                    add(task,'resume',cfg['resume_chat_id'],record['sha256'],file_id=record['id'],case_id=record['case_id'],candidate_id=record['candidate_id'])
        if cfg['todo_user_id']:
            for todo in todos:
                if todo['status']!='resolved' and _owns_todo(manager,task,todo):
                    add(task,'todo',cfg['todo_user_id'],todo['id'],todo_id=todo['id'],case_id=todo.get('case_id'))
                    if pause_alert(manager,task['id'],todo):
                        chain=[existing.get(_key([task['id'],'todo',cfg['todo_user_id'],todo['id']]))]
                        n=1
                        while True:
                            item=existing.get(_key([task['id'],'todo',cfg['todo_user_id'],todo['id']+':reminder:'+str(n)]))
                            if not item:break
                            chain.append(item);n+=1
                        chain=[i for i in chain if i]
                        due=_reminder_due(manager,task,todo,chain,time.time())
                        if due:add(task,'todo',cfg['todo_user_id'],todo['id']+':reminder:'+str(len(chain)),todo_id=todo['id'],case_id=todo.get('case_id'),reminder=due)


def _current(manager,item):
    try: task=manager.definitions.get(item['task_id'])
    except FileNotFoundError: return None
    cfg=validate_config(task['config'].get('notifications'))
    field='resume_chat_id' if item['kind']=='resume' else 'todo_user_id'
    if not cfg['enabled'] or cfg[field]!=item['destination']: return None
    if item['kind']=='resume':
        if item['case_id'] not in task['config'].get('case_ids',[]): return None
    else:
        try: todo=manager.db.get('attention',item['todo_id'])
        except FileNotFoundError: return None
        if todo['status']=='resolved' or not _owns_todo(manager,task,todo): return None
        if item.get('reminder') and not pause_alert(manager,task['id'],todo): return None
    return task


def _process(manager,client,item):
    db=manager.db
    if item['status']=='sent' or not _bind_application(db,item): return
    if item['status'] in ('sending','unknown') or item.get('send_uncertain'):
        item.update(status='unknown',send_uncertain=True,error='发送被中断或结果未确认，保留原记录；未重复推送');_save(db,item);return
    if not _current(manager,item):
        item.update(status='cancelled',error='任务通知已关闭、接收目标变化或待办已处理');_save(db,item);return
    item.update(status='preparing',attempts=item['attempts']+1,error=None);_save(db,item)
    phase='prepare'
    try:
        task=_current(manager,item)
        if not task: return
        _check_application(item)
        client.authenticate()
        if item['kind']=='resume':
            from .resume_archive import read_file
            record,data=read_file(manager.cases,item['file_id'])
            if record['sha256']!=item['source']: raise DeliveryError('简历文件摘要变化，未推送')
            if not item.get('file_key'):
                item['file_key']=client.upload(record,data);_save(db,item)
            case=manager.cases.load(item['case_id']);person=case['candidates'][item['candidate_id']]
            label=person.get('profile',{}).get('platform_label') or person.get('profile',{}).get('display_name') or '候选人'
            from .resume_scoring import evaluation_lines
            metadata='收到简历\n任务：'+task['config']['name']+'\n岗位：'+case['job']['label']+'\n候选人：'+label+'\n文件：'+record['filename']+'\n'+evaluation_lines(item.get('evaluation'))
            parts=[('file','file',{'file_key':item['file_key']}),('metadata','text',{'text':metadata})]
            receiver_type='chat_id'
        else:
            todo=db.get('attention',item['todo_id'])
            paused_at=pause_alert(manager,task['id'],todo)
            if paused_at:
                from zoneinfo import ZoneInfo
                from datetime import datetime
                clock=datetime.fromtimestamp(paused_at,ZoneInfo(_schedule(task).get('timezone','Asia/Shanghai'))).strftime('%m-%d %H:%M')
                body=('【任务已暂停】'+task['config']['name']+'\n暂停时间：'+clock+('（第 '+str(item['reminder'])+' 次提醒，仍未处理）' if item.get('reminder') else '')
                      +'\n影响：暂停期间不收件、不回复候选人\n原因：'+todo['title']+'\n'+(todo.get('detail') or '')
                      +'\n处理：打开工作台 http://127.0.0.1:'+os.environ.get('CLOAKUP_PUBLIC_PORT','8766')+' → 管理员待办\n待办编号：'+todo['id'])
                parts=[('message','text',{'text':body})];receiver_type='open_id'
            else:
                body='待办提醒\n任务：'+task['config']['name']+'\n'+todo['title']
                if todo.get('job_label'): body+='\n岗位：'+todo['job_label']
                if todo.get('question'): body+='\n候选人问题：'+todo['question'][:600]
                if todo.get('kind')=='execution_recovery':
                    body+='\n恢复尝试：'+str(todo.get('recovery_attempts',0))+'/'+str(todo.get('recovery_max_attempts',3))
                    body+='\n'+todo['detail']
                body+='\n请在 cloakup 平台待办中心处理。\n待办编号：'+todo['id']
                parts=[('message','text',{'text':body})];receiver_type='open_id'
        for part,msg_type,content in parts:
            if item.get(part+'_message_id'): continue
            if not _current(manager,item):
                item.update(status='cancelled',error='发送前任务通知目标或待办状态已变化');_save(db,item);return
            # Auth failure must not be classified as an ambiguous message write.
            _check_application(item)
            client.authenticate()
            _check_application(item)
            item.update(status='sending',sending_part=part,send_uncertain=True);_save(db,item);phase='send'
            message_id=client.send(item['destination'],receiver_type,msg_type,content,_key([item['app_id'],item['id'],part])[:32])
            item[part+'_message_id']=message_id
            item.update(status='preparing',sending_part=None,send_uncertain=False);_save(db,item);phase='prepare'
        item.update(status='sent',sent_at=time.time(),error=None);_save(db,item)
    except DeliveryError as exc:
        if phase=='send' and not exc.ambiguous: item['send_uncertain']=False
        item.update(status='unknown' if exc.ambiguous else 'retry' if exc.retryable else 'failed',error=str(exc),next_attempt_at=time.time()+min(3600,60*2**min(item['attempts'],5)))
        _save(db,item)
    except Exception:
        # Never leak a file path, token, API body, or candidate content in errors.
        item.update(status='unknown' if phase=='send' else 'failed',error='通知结果待核实，未重复发送' if phase=='send' else '无法读取或准备通知，请检查本地简历归档与任务配置')
        _save(db,item)


async def tick(manager):
    """Call from the existing monitor. One pending item per tick; no browser use."""
    if not hasattr(manager,'_notification_lock'):manager._notification_lock=asyncio.Lock()
    if manager._notification_lock.locked():return
    async with manager._notification_lock:
        await asyncio.to_thread(enqueue,manager)
        if not hasattr(manager,'_notification_client'):manager._notification_client=FeishuClient()
        client=manager._notification_client
        queue=sorted(manager.db.list(KIND),key=lambda i:i['created_at'])
        if not client.configured():
            for item in queue:
                if item['status']=='sending':
                    item.update(status='unknown',error='发送被中断，保留原记录；未重复推送');_save(manager.db,item)
                elif item['status'] in ('queued','preparing','retry','waiting_credentials'):
                    state='waiting_credentials' if _current(manager,item) else 'cancelled'
                    detail='等待服务端配置飞书应用凭据，尚未发送' if state=='waiting_credentials' else '任务通知已关闭、接收目标变化或待办已处理'
                    if item['status']!=state or item.get('error')!=detail:
                        item.update(status=state,error=detail,next_attempt_at=0);_save(manager.db,item)
            return
        for item in queue:
            if item['status'] in ('queued','preparing','sending','retry','waiting_credentials') and item.get('next_attempt_at',0)<=time.time():
                if await _ensure_evaluation(manager,item):
                    await asyncio.to_thread(_process,manager,client,item)
                break


MAX_EVALUATION_ATTEMPTS = 3


async def _ensure_evaluation(manager,item):
    """简历在发送说明前评分一次，结果写入记录；之后的重试复用结果，不再调用模型。

    返回 False 表示评分暂时失败、已安排稍后重试，本轮不发送。
    """
    if item['kind']!='resume' or 'evaluation' in item or item.get('metadata_message_id'):
        return True
    from .resume_archive import read_file
    from .resume_scoring import ScoringUnavailable, evaluate
    attempts=item.get('evaluation_attempts',0)+1
    try:
        case=manager.cases.load(item['case_id']);person=manager.cases.candidate(case,item['candidate_id'])
        record,data=read_file(manager.cases,item['file_id'])
        if record['sha256']!=item['source']:raise ScoringUnavailable('简历文件摘要变化，未评分')
        item['evaluation']=await evaluate(manager,case,person,record,data)
    except ScoringUnavailable as exc:
        if exc.retryable and attempts<MAX_EVALUATION_ATTEMPTS:
            item.update(evaluation_attempts=attempts,status='retry',error='评分暂不可用，稍后重试',next_attempt_at=time.time()+min(3600,60*2**attempts))
            _save(manager.db,item);return False
        item['evaluation']={'status':'unavailable','reason':str(exc)[:200],'at':time.time()}
    except Exception:
        # 不把简历内容或服务端错误细节写入通知记录。
        item['evaluation']={'status':'unavailable','reason':'读取简历或评分失败，已跳过评分','at':time.time()}
    item['evaluation_attempts']=attempts
    _save(manager.db,item)
    return True


def status(manager):
    configured=bool(os.environ.get('LARK_APP_ID') and os.environ.get('LARK_APP_SECRET'))
    records=manager.db.list(KIND)
    fields=('id','task_id','kind','destination','status','error','attempts','created_at','updated_at','sent_at','file_id','todo_id','file_message_id','metadata_message_id','message_message_id')
    return {'identity':'bot','configured':configured,
            'app_id_configured':bool(os.environ.get('LARK_APP_ID')),'app_secret_configured':bool(os.environ.get('LARK_APP_SECRET')),
            'detail':'应用凭据已配置；发送时核验飞书权限和接收目标' if configured else '请配置飞书应用 LARK_APP_ID 与 LARK_APP_SECRET，并在任务中设置接收群和用户',
            'counts':{state:sum(i['status']==state for i in records) for state in ('queued','waiting_credentials','preparing','sending','retry','sent','unknown','failed','cancelled')},
            'records':[{k:i[k] for k in fields if k in i} for i in records[:100]]}
