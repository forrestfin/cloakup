"""寻访任务的每日首聊额度：账号硬上限、任务当天目标、按岗位权重分份额并选出下一个首聊岗位。

计数按占位：当天占位的消息（含送达未知）与平台外观察到的当天首聊，口径与 `autopilot.daily_attempts` 共用 `attempted_on`。
"""
from __future__ import annotations

import copy

from . import job_members
from .autonomy import day_key
from .pacing import stable_fraction

ACCOUNT_DAILY_CAP=20


def attempted_on(m:dict,day:str,intent:str|None=None)->bool:
    """这条消息是否算作 day 当天的一次占位。"""
    if intent and m.get('intent')!=intent:return False
    if m.get('claimed_at'):return day_key(m['claimed_at'])==day
    return m.get('origin')=='external_observed' and m.get('intent')=='first_contact' and str(m.get('receipt',{}).get('platform_time','')).startswith(day)


def task_target(task_id:str,limit:int,day:str)->int:
    """任务当天首聊目标：上限的 75%～100%，按“日期+任务”稳定取值；上限大于 0 时至少 1。"""
    if limit<=0:return 0
    return max(1,round(limit*(0.75+0.25*stable_fraction('daily_new',day,task_id))))


def allocate(total:int,weights:dict[str,int])->dict[str,int]:
    """最大余数法按权重分 total；余数相同按 case_id 排序，结果稳定且合计恰为 total。"""
    if not weights:return {}
    whole=sum(weights.values());raw={k:total*w/whole for k,w in weights.items()}
    shares={k:int(v) for k,v in raw.items()};rest=total-sum(shares.values())
    for k in sorted(raw,key=lambda k:(-(raw[k]-shares[k]),k))[:rest]:shares[k]+=1
    return shares


def usage_by_case(store,case_ids:list[str],day:str)->dict[str,int]:
    """给定岗位当天的首聊占位数。"""
    usage={cid:0 for cid in case_ids}
    for case in store.list():
        if case['id'] not in usage:continue
        usage[case['id']]+=sum(attempted_on(m,day,'first_contact') for c in case['candidates'].values() for m in c['outbox'])
    return usage


def pick_case(task_id:str,limit:int,weights:dict[str,int],usage:dict[str,int],exhausted:set[str],day:str)->str|None:
    """选下一个首聊岗位；usage 含当天属于本任务的全部岗位（包括已撤下的），合计达到任务目标即不再选。"""
    target=task_target(task_id,limit,day)
    if sum(usage.values())>=target:return None
    shares=allocate(target,weights);open_=[k for k in weights if k not in exhausted]
    under=[k for k in open_ if usage.get(k,0)<shares[k]]
    if under:return min(under,key=lambda k:(usage.get(k,0)/shares[k],-weights[k],k))
    # 份额都已用完但任务未达目标：其他岗位让出的额度按权重再分
    return min(open_,key=lambda k:(usage.get(k,0)/weights[k],-weights[k],k)) if open_ else None


def counted_case_ids(cfg:dict,day:str)->list[str]:
    """当天计入任务合计的岗位：全部成员（含已撤下）加上当天被移除的岗位，移除后当天额度不重置。"""
    ids=[m['case_id'] for m in job_members.normalize(copy.deepcopy(cfg))]
    removed=[e['case_id'] for e in cfg.get('removed_today') or [] if isinstance(e,dict) and e.get('day')==day and isinstance(e.get('case_id'),str)]
    return list(dict.fromkeys(ids+removed))


def account_exhausted(store,day:str)->bool:
    from .autopilot import daily_attempts
    return daily_attempts(store,'first_contact',day)>=ACCOUNT_DAILY_CAP


def allowed(store,task:dict,case_id:str,day:str)->bool:
    """首聊入口的保守复核：账号未满、任务未达目标，且该岗位参与首聊、在其他岗位都无人可联系时可被选中。"""
    if account_exhausted(store,day):return False
    cfg=copy.deepcopy(task['config']);job_members.normalize(cfg);weights=job_members.weights(cfg)
    if case_id not in weights:return False
    usage=usage_by_case(store,counted_case_ids(cfg,day),day)
    limit=cfg.get('execution',{}).get('daily_new_limit',0)
    return pick_case(task['id'],limit,weights,usage,set(weights)-{case_id},day)==case_id


def task_quota(store,task:dict,day:str,*,usage:dict[str,int]|None=None,account_used:int|None=None)->dict:
    """界面用的只读额度视图：任务总额、今日目标、任务与账号当天已用，各参与首聊岗位的份额与各岗位已用（含已撤下、当天移除）。

    usage（全部岗位当天占位数）与 account_used 可由调用方一次算好后在多个任务间共用，口径与 usage_by_case、daily_attempts 相同。
    """
    from .autopilot import daily_attempts
    cfg=copy.deepcopy(task['config']);job_members.normalize(cfg)
    limit=cfg.get('execution',{}).get('daily_new_limit',0);target=task_target(task['id'],limit,day);counted=counted_case_ids(cfg,day)
    usage={cid:usage.get(cid,0) for cid in counted} if usage is not None else usage_by_case(store,counted,day)
    return {'limit':limit,'target':target,'used':sum(usage.values()),'account_used':daily_attempts(store,'first_contact',day) if account_used is None else account_used,
            'account_cap':ACCOUNT_DAILY_CAP,'shares':allocate(target,job_members.weights(cfg)),'usage':usage}
