import pytest
from types import SimpleNamespace as NS
from cloakup.boss import validate_job_url, job_from_observation, prepare_plan
from cloakup.observe import build_observation
from cloakup.sourcing import SourcingError


def test_job_read_requires_complete_real_iframe_fields():
    raw = {'url':'https://www.zhipin.com/web/chat/job/edit?encryptId=test-job', 'title':'BOSS', 'elements':[], 'total':0}
    for tag,label,value in [('input','请填写职位名称','高端猎聘交付达人'),('textarea','职位描述','至少5年从业经验，需有实际高端岗位交付业绩。出海行业经验加分。'),('input','选择工作地点','武汉')]:
        raw['elements'].append({'tag':tag,'label':label,'value':value,'css':tag,'x':0,'y':0,'frame_url':'https://www.zhipin.com/web/frame/job/edit?encryptId=test-job'})
    snapshot = job_from_observation(build_observation(raw))
    assert snapshot['title'] == '高端猎聘交付达人'
    assert '交付业绩' in snapshot['requirements']
    assert snapshot['location'] == '武汉'
    raw['elements'][1]['value'] += '…'
    with pytest.raises(SourcingError): job_from_observation(build_observation(raw))


def test_job_url_cannot_navigate_arbitrary_site_or_create_page():
    assert validate_job_url('https://www.zhipin.com/web/chat/job/edit?encryptId=a')
    for value in ['https://attacker.test/web/chat/job/edit?encryptId=a','https://www.zhipin.com/web/chat/job/list','https://www.zhipin.com/web/chat/job/edit']:
        with pytest.raises(SourcingError): validate_job_url(value)


async def test_plan_is_draft_based_on_job_evidence():
    class LLM:
        async def json(self, **kwargs):
            assert '高端岗位交付业绩' in kwargs['content']
            assert '不发送消息' in kwargs['system']
            assert '候选人的背景' in kwargs['system']
            return {'greeting':'请问方便了解这个岗位吗？','keywords':['猎头顾问'],'requirements':[], 'follow_up_questions':[]}
    result = await prepare_plan(LLM(), {'title':'招聘顾问','requirements':'高端岗位交付业绩'})
    assert result['greeting']
