"""寻访任务的岗位成员：每个岗位的配额权重与首聊状态。

所有成员都参与收件与回复；只有 outreach=='active' 的岗位按权重分配首聊。
`case_ids` 与成员保持一致，保留给按旧字段读写的代码：只改了 case_ids 时成员随之增删。
"""
from __future__ import annotations

OUTREACH=('active','withdrawn')


def normalize(cfg:dict)->list[dict]:
    """校验并写回 cfg['members'] 与 cfg['case_ids']；无成员时由 case_ids 生成（权重 1、参与首聊）。

    members 与 case_ids 不一致时以 case_ids 决定成员与顺序：保留下来的岗位沿用原权重与首聊状态，新岗位为权重 1、参与首聊。
    """
    raw,ids=cfg.get('members'),cfg.get('case_ids')
    if ids is not None and (not isinstance(ids,list) or any(not isinstance(i,str) for i in ids)):raise ValueError('请选择1至20个岗位')
    if raw is None:raw=[{'case_id':i,'weight':1,'outreach':'active'} for i in ids or []]
    elif ids is not None and isinstance(raw,list) and all(isinstance(m,dict) for m in raw) and ids!=[m.get('case_id') for m in raw]:
        # 导入岗位、草案编辑等旧写法只改 case_ids：成员随之增删，已有岗位保留权重与首聊状态
        old={m.get('case_id'):m for m in raw};raw=[old.get(i,{'case_id':i,'weight':1,'outreach':'active'}) for i in ids]
    if not isinstance(raw,list) or not 1<=len(raw)<=20:raise ValueError('请选择1至20个岗位')
    members=[]
    for m in raw:
        if not isinstance(m,dict) or not isinstance(m.get('case_id'),str) or not m['case_id']:raise ValueError('岗位编号不合法')
        weight=m.get('weight',1)
        if type(weight) is not int or not 1<=weight<=10:raise ValueError('岗位权重须为1至10的整数')
        outreach=m.get('outreach','active')
        if outreach not in OUTREACH:raise ValueError('岗位首聊状态不合法')
        members.append({'case_id':m['case_id'],'weight':weight,'outreach':outreach})
    ids=[m['case_id'] for m in members]
    if len(set(ids))!=len(ids):raise ValueError('同一岗位不能重复加入任务')
    cfg['members']=members;cfg['case_ids']=ids
    return members


def outreach_ids(cfg:dict)->list[str]:
    """参与首聊的岗位。"""
    return [m['case_id'] for m in normalize(dict(cfg)) if m['outreach']=='active']


def weights(cfg:dict)->dict[str,int]:
    """参与首聊岗位的配额权重。"""
    return {m['case_id']:m['weight'] for m in normalize(dict(cfg)) if m['outreach']=='active'}
