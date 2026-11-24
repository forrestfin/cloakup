import pytest
from cloakup.answering import answer_question

@pytest.fixture
def case():return {'config_revision':7,'anonymous_job':{'requirements':['招聘或猎头经验至少五年，本科及以上学历。','远程协作，地区不限，华中区域优先。','负责人才寻访与交付。']}}

@pytest.mark.parametrize('question',['请问工作地方在哪','工作地点在哪里？','在哪里办公？','这个岗位可以远程吗？'])
def test_known_work_arrangement_is_answered_from_confirmed_fact(case,question):
 result=answer_question(case,question)
 assert result['action']=='answer' and result['source']=='job_facts'
 assert '远程协作，地区不限' in result['text'] and result['fact_indices']==[1]
 assert result['config_revision']==7

@pytest.mark.parametrize('text',['我本科毕业','互联网 AI 机器人方向','招聘工作超过五年了','好的','可以聊聊','在吗？','你好？'])
def test_professional_statement_is_not_a_question(case,text):
 assert answer_question(case,text)['action']=='continue'

@pytest.mark.parametrize('question',['工作地点和薪资是多少？','需要出差吗？','保证完全不用坐班吗？','什么时间上班？'])
def test_uncovered_or_sensitive_parts_are_not_inferred(case,question):
 assert answer_question(case,question)['action']=='defer'


def test_missing_or_multiple_location_facts_do_not_guess(case):
 case['anonymous_job']['requirements']=[]
 assert answer_question(case,'工作地点在哪里？')['action']=='defer'
 case['anonymous_job']['requirements']=['工作地点在武汉。','工作地点在上海。']
 assert answer_question(case,'工作地点在哪里？')['action']=='defer'


def test_known_location_does_not_create_a_false_administrator_todo(case,tmp_path):
 from cloakup.attention import AttentionStore
 from cloakup.storage import Database
 case.update(id='case',job={'label':'招聘岗位'},candidates={'p':{'id':'p','inbox':[{'external_id':'q','text':'请问工作地方在哪'}]}})
 assert not AttentionStore(Database(tmp_path)).reconcile([], [case])


def test_extra_uncovered_question_is_not_hidden_by_location_match(case):
 assert answer_question(case,'请问工作地点和住宿情况？')['action']=='defer'


def test_salary_year_is_never_experience_evidence(case):
 case['anonymous_job']['requirements']=['负责百万年薪级人才寻访。']
 assert answer_question(case,'需要几年工作经验？')['action']=='defer'


def test_typed_fact_scope_status_and_type_are_required(case):
 case['confirmed_facts']=[{'id':'f','kind':'duties','value':'有五年工作经验','scope':'job','status':'confirmed','source':'administrator','revision':1}]
 assert answer_question(case,'需要几年工作经验？')['action']=='defer'
 fact=case['confirmed_facts'][0];fact.update(kind='experience',status='unconfirmed')
 assert answer_question(case,'需要几年工作经验？')['action']=='defer'
 fact['status']='confirmed';result=answer_question(case,'需要几年工作经验？')
 assert result['action']=='answer' and result['sources'][0]['id']=='f' and result['sources'][0]['revision']==1
 fact['scope']='other-job'
 assert answer_question(case,'需要几年工作经验？')['action']=='defer'
import pytest


@pytest.mark.parametrize('text',['Boss，您好，可以聊聊吗？','方便聊聊吗？','您好，可以了解一下吗？'])
def test_conversation_invitation_is_not_missing_business_fact(text):
 from cloakup.answering import answer_question
 assert answer_question({},text)['action']=='continue'


@pytest.mark.parametrize('text',['您好，可以聊聊吗？底薪多少？','方便聊聊具体上班时间吗？'])
def test_invitation_does_not_mask_actual_business_question(text):
 from cloakup.answering import answer_question
 assert answer_question({},text)['action']!='continue'

