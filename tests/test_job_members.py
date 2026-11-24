"""寻访任务的岗位成员：权重与首聊状态。"""
import pytest
from cloakup.job_members import normalize, outreach_ids, weights


def test_legacy_case_ids_become_active_members():
    cfg={'case_ids':['a','b']}
    assert normalize(cfg)==[{'case_id':'a','weight':1,'outreach':'active'},{'case_id':'b','weight':1,'outreach':'active'}]
    assert cfg['members']==normalize(cfg) and cfg['case_ids']==['a','b']


def test_members_drive_case_ids_and_outreach():
    cfg={'members':[{'case_id':'a','weight':3,'outreach':'active'},{'case_id':'b','weight':2,'outreach':'withdrawn'}]}
    normalize(cfg)
    assert cfg['case_ids']==['a','b'] and outreach_ids(cfg)==['a'] and weights(cfg)=={'a':3}


def test_legacy_writer_editing_case_ids_keeps_member_attributes():
    # 11-24 导入岗位、草案编辑仍只改 case_ids：成员随 case_ids 增删，已有岗位的权重与首聊状态保留
    cfg={'case_ids':['b','c'],'members':[{'case_id':'a','weight':3,'outreach':'active'},{'case_id':'b','weight':2,'outreach':'withdrawn'}]}
    assert normalize(cfg)==[{'case_id':'b','weight':2,'outreach':'withdrawn'},{'case_id':'c','weight':1,'outreach':'active'}] and cfg['case_ids']==['b','c']


def _members(n):return [{'case_id':f'c{i}','weight':1,'outreach':'active'} for i in range(n)]


@pytest.mark.parametrize('members',[
    [{'case_id':'a','weight':0,'outreach':'active'}],
    [{'case_id':'a','weight':11,'outreach':'active'}],
    [{'case_id':'a','weight':'2','outreach':'active'}],
    [{'case_id':'a','weight':True,'outreach':'active'}],
    [{'case_id':'a','weight':1,'outreach':'active'},{'case_id':'a','weight':2,'outreach':'active'}],
    [], _members(21),
    [{'case_id':'a','weight':1,'outreach':'x'}],
    [{'case_id':3,'weight':1,'outreach':'active'}],
    'a',
])
def test_invalid_members_rejected(members):
    with pytest.raises(ValueError):normalize({'members':members})
