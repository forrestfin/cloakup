from types import SimpleNamespace as NS
import json
import pytest
import jsonschema
from cloakup.recruiting_ai import APPROVED_HOST, analyze_anonymous_brief, checked_brief
from cloakup.sourcing import SourcingError


def brief():
    return {'job':{'role':'猎聘交付顾问','requirements':['至少五年从业经验，具备高端交付业绩']},
            'candidate':{'experience':'资料自述五年猎头经验','availability':'在职，暂不考虑机会',
                         'visible_evidence':'可见经历包含面试协调，未见量化业绩','coverage':'仅当前可见部分'}}


async def test_only_approved_anonymous_text_leaves_boundary():
    calls=[]
    class LLM:
        client=NS(raw=NS(base_url='https://'+APPROVED_HOST+'/api/plan/'))
        async def json(self,**kwargs):
            calls.append(kwargs)
            assert isinstance(kwargs['content'],str)
            assert json.loads(kwargs['content'])==brief()
            return {'draft':'如果未来有兴趣，欢迎交流。'}
    result=await analyze_anonymous_brief(LLM(),brief(),consent={'mode':'anonymous_job_text','host':APPROVED_HOST})
    assert result['draft'] and len(calls)==1
    assert 'image' not in calls[0]['content']


async def test_consent_or_destination_change_blocks_before_any_request():
    class LLM:
        client=NS(raw=NS(base_url='https://other-model.example/'))
        async def json(self,**kwargs): raise AssertionError('must not send')
    with pytest.raises(SourcingError):
        await analyze_anonymous_brief(LLM(),brief(),consent={})
    with pytest.raises(SourcingError):
        await analyze_anonymous_brief(LLM(),brief(),consent={'mode':'anonymous_job_text','host':APPROVED_HOST})


@pytest.mark.parametrize('key,value',[('name','真实姓名'),('age',30),('gender','男'),('company','任职公司'),('screenshot',{'type':'image'}),('platform_id','123')])
def test_raw_profile_fields_are_rejected(key,value):
    value_brief=brief();value_brief['candidate'][key]=value
    with pytest.raises(jsonschema.ValidationError): checked_brief(value_brief)


@pytest.mark.parametrize('value',['https://www.zhipin.com/profile/123','13812345678','user@example.com','张**','某某有限公司','30岁'])
def test_common_identifiers_in_text_are_rejected(value):
    value_brief=brief();value_brief['candidate']['experience']=value
    with pytest.raises(SourcingError): checked_brief(value_brief)
