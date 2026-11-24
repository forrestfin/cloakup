"""管理员在待办中确认的岗位问答：加入本岗位常见问题，并在执行器安全点生效。

闭环：候选人问题缺少依据 -> 生成待办 -> 管理员填写脱敏问法与已确认答案 ->
本模块校验后排队 -> 执行器在每轮开始（未持有任何岗位数据时）写入生效版本与岗位 ->
FAQ 进入对话理解模型的已确认来源，搁置的问题在下一轮被重新理解并作答 -> 待办自动解除。

FAQ 属于各岗位自己的配置，代码不内置任何岗位内容；原问题只保存在本机，不进入 FAQ。
"""
import copy
import time
import uuid

from .faq import checked_faq
from .sourcing import SourcingError, TERMINAL

KIND = 'faq_application'


def _blank_candidate():
    return {k: '' for k in ('experience', 'availability', 'visible_evidence', 'coverage')}


def checked_answer(question, answer):
    """与 source_catalog 相同的准入条件：不满足时模型看不到这条 FAQ，闭环会静默失败，因此提交时就拒绝。"""
    from .answering import UNCERTAIN
    from .communication_policy import check_compensation_disclosure
    from .recruiting_ai import checked_brief
    question = (question or '').strip();answer = (answer or '').strip()
    if not 1 <= len(question) <= 200:raise ValueError('请填写1至200字的脱敏通用问法')
    if not 1 <= len(answer) <= 2000:raise ValueError('请填写1至2000字的已确认答案')
    if UNCERTAIN.search(answer):raise ValueError('答案含“待确认/待核实”等不确定表述，模型不会采用；请填写已确认的内容')
    try:
        check_compensation_disclosure(answer)
        checked_brief({'job': {'role': '岗位问答', 'requirements': [question, answer]}, 'candidate': _blank_candidate()})
    except SourcingError as exc:
        raise ValueError(str(exc)) from exc
    return question, answer


def upsert(faq, question, answer):
    """按问法新增或更新一条 FAQ；保留其他条目与相同问法。"""
    items = copy.deepcopy(faq or [])
    entry = next((f for f in items if f['question'] == question), None)
    if entry:entry.update(answer=answer, enabled=True)
    else:items.append({'question': question, 'answer': answer, 'variants': [], 'enabled': True})
    try:checked_faq(items)
    except SourcingError as exc:raise ValueError(str(exc)) from exc
    return items


def submit(manager, task_id, body):
    """校验待办答案并排队；执行器未运行时立即生效。"""
    from .conversation_semantics import redact
    db = manager.db
    todo = db.get('attention', body.get('todo_id'))
    task = manager.definitions.get(task_id)
    if todo.get('kind') != 'question' or todo['status'] == 'resolved' or todo.get('case_id') not in task['config']['case_ids']:
        raise ValueError('该待办不属于任务或不需要补充答案')
    if body.get('reviewed') is not True:raise ValueError('请先确认答案真实，并复核问答已脱敏')
    if any(r['status'] == 'pending' and r['todo_id'] == todo['id'] for r in db.list(KIND)):
        raise ValueError('该待办的答案正在等待生效，请勿重复提交')
    case = manager.cases.load(todo['case_id']);person = manager.cases.candidate(case, todo['candidate_id'])
    try:
        question = redact(body.get('question', ''), person);answer = redact(body.get('answer', ''), person)
    except SourcingError as exc:
        raise ValueError(str(exc)) from exc
    question, answer = checked_answer(question, answer)
    upsert(case.get('faq', []), question, answer)
    manager.agent_workspace.bootstrap(task_id)
    now = time.time()
    record = {'id': uuid.uuid4().hex, 'task_id': task_id, 'case_id': todo['case_id'], 'todo_id': todo['id'],
              'candidate_id': todo['candidate_id'], 'reply_id': todo.get('reply_id'), 'question': question, 'answer': answer,
              'status': 'pending', 'at': now}
    db.put(KIND, record['id'], record)
    todo.update(status='in_progress', updated_at=now, faq_answer={'id': record['id'], 'status': 'pending', 'question': question, 'at': now})
    todo['history'].append({'at': now, 'action': 'faq_answer_submitted', 'note': '答案已加入岗位常见问题，等待执行器安全点生效'})
    db.put('attention', todo['id'], todo)
    # 没有活动运行时没有其他写入方，可以立即生效；否则由执行器在下一轮开始时应用，避免整份岗位数据互相覆盖。
    if not manager.busy():apply_pending(manager)
    return db.get(KIND, record['id'])


def apply_pending(manager):
    """执行器安全点调用：依次应用排队的问答。单条失败只标记该条，不影响执行器。"""
    db = getattr(manager, 'db', None)
    if db is None or not hasattr(db, 'list'):return 0
    applied = 0
    for record in sorted((r for r in db.list(KIND) if r['status'] == 'pending'), key=lambda r: r['at']):
        try:
            apply(manager, record);applied += 1
        except Exception as exc:
            record.update(status='failed', error=str(exc)[:400] or type(exc).__name__, finished_at=time.time())
            db.put(KIND, record['id'], record)
            _mark_todo(db, record, 'failed', '答案未能生效：' + record['error'])
    realign_owed_questions(manager)
    return applied


def realign_owed_questions(manager):
    """安全点幂等纠正：待答指针指向旧问题、而最新问题只回过“确认收到”时，改指最新问题，下一轮补答。"""
    from .decision_runtime import restore_owed_answer
    store = getattr(manager, 'cases', None)
    if store is None or not hasattr(store, 'list'):return 0
    fixed = 0
    try:
        for case in store.list():
            for person in list(case.get('candidates', {}).values()):
                if person.get('pending_question_id') and restore_owed_answer(store, case, person):fixed += 1
    except Exception:
        return fixed
    return fixed


def apply(manager, record):
    """把问答写入生效版本（派生自已验收版本，仅 FAQ 不同）与岗位配置，原子提交。

    不提升岗位 config_revision：任务计划签名依赖它，提升会使当前计划失效。
    对话理解缓存以 FAQ 内容为键，FAQ 变化后自动重新理解。
    """
    db = manager.db;now = time.time()
    release = manager.agent_workspace.bootstrap(record['task_id'])
    base = db.get('agent_bundle', release['version_id'])
    bundle = copy.deepcopy(base['bundle'])
    item = next((s for s in bundle['sources'] if s['case_id'] == record['case_id']), None)
    if item is None:raise ValueError('岗位已不属于该任务的生效版本')
    case = manager.cases.load(record['case_id'])
    if any(m['status'] == 'sending' for p in case['candidates'].values() for m in p.get('outbox', [])):
        raise ValueError('该岗位仍有消息发送中，稍后重试')
    item['faq'] = upsert(item['faq'], record['question'], record['answer'])
    version_id = uuid.uuid4().hex
    version = {'id': version_id, 'task_id': record['task_id'], 'bundle': bundle, 'digest': _digest(bundle), 'created_at': now,
               'origin': 'admin_todo_answer', 'validation': base.get('validation'), 'test_id': base.get('test_id'),
               'base_version_id': base['id'], 'tested_version_id': base.get('tested_version_id') or base['id'],
               'faq_application_id': record['id']}
    next_release = {**release, 'version_id': version_id, 'revision': release['revision'] + 1, 'released_at': now, 'faq_application_id': record['id']}
    case['faq'] = copy.deepcopy(item['faq']);case['agent_release_id'] = version_id;case['updated_at'] = now
    person = case['candidates'].get(record['candidate_id'])
    if person and person.get('status') not in TERMINAL and record.get('reply_id'):
        # 已指向更新的待答问题时保留（11-24 候选人 30：后补的旧问题把最新问题的指针覆盖了）。
        from .decision_runtime import later_question
        person['pending_question_id'] = later_question(person, person.get('pending_question_id'), record['reply_id'])
    # 所有仍在等待管理员答复的候选人都按新 FAQ 重新理解：同一问题适用于本岗位全部候选人。
    for p in case['candidates'].values():
        if p.get('pending_question_id') and p.get('status') not in TERMINAL:
            p.pop('conversation_semantics', None);p.pop('harness_decision', None);p.pop('decision_wait_until', None)
    record = {**record, 'status': 'applied', 'applied_at': now, 'version_id': version_id}
    items = [('agent_bundle', version_id, version, 0, True),
             ('agent_release', record['task_id'], next_release, db.revision('agent_release', record['task_id']), True),
             ('case', case['id'], case, db.revision('case', case['id']), True),
             (KIND, record['id'], record, db.revision(KIND, record['id']), False)]
    # 运行中的任务绑在旧版本上：新版本只多了管理员确认的这条问答，由执行器在安全点写入，
    # 同一事务里把任务绑到新版本。否则 availability 判定 release_changed，任务显示运行却不再发送。
    member = next((m for m in db.list('account_task') if m['id'] == record['task_id']), None)
    if member and member.get('release_id') == release['version_id']:
        rebound = {**member, 'release_id': version_id}
        rebound['history'] = [*member.get('history', []), {'at': now, 'status': member['status'], 'release_id': version_id, 'reason': 'faq_answer_applied'}]
        items.append(('account_task', member['id'], rebound, db.revision('account_task', member['id']), True))
    db.put_batch(items)
    # 审计会整份保存岗位，须在原子提交之后写入。
    case = manager.cases.load(record['case_id'])
    manager.cases.audit(case, 'admin_answer_confirmed', candidate_id=record['candidate_id'], reply_id=record.get('reply_id'),
                        faq_application_id=record['id'], version_id=version_id, answer=record['answer'])
    _mark_todo(db, record, 'applied', '答案已加入岗位常见问题并生效，执行端下一轮按新事实答复')
    return record


def _digest(bundle):
    from .agent_workspace import digest
    return digest(bundle)


def _mark_todo(db, record, status, note):
    try:todo = db.get('attention', record['todo_id'])
    except FileNotFoundError:return
    todo['faq_answer'] = {**todo.get('faq_answer', {}), 'id': record['id'], 'status': status, 'error': record.get('error'), 'updated_at': time.time()}
    todo['history'].append({'at': time.time(), 'action': 'faq_answer_' + status, 'note': note})
    db.put('attention', todo['id'], todo)
