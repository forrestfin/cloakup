import pytest
from cloakup.communication_policy import check_compensation_disclosure
from cloakup.sourcing import SourcingError

@pytest.mark.parametrize('text',['固定底薪8000元，提成10%','月薪8-12K','年薪30万元','薪酬提供13薪','提成为百分之二十','底薪八千元'])
def test_numeric_compensation_is_blocked(text):
 with pytest.raises(SourcingError):check_compensation_disclosure(text)

@pytest.mark.parametrize('text',['固定底薪＋绩效工资＋提成，整体方案合理、有竞争力，具体可以详聊。','负责百万年薪级人才寻访与交付。','需要5年招聘经验。'])
def test_structure_and_professional_experience_are_allowed(text):check_compensation_disclosure(text)

def test_old_numeric_faq_is_not_used_for_public_answer():
 from cloakup.answering import answer_question
 c={'faq':[{'question':'薪资如何','answer':'月薪8000元','enabled':True,'variants':[]}]}
 assert answer_question(c,'薪资如何')['action']=='defer'

@pytest.mark.parametrize('question',['底薪是多少','底薪大概多少','提成比例是多少','月薪能给多少'])
def test_salary_quantity_followup_is_policy_answer_not_missing_fact(question):
 from cloakup.answering import answer_question
 c={'faq':[{'question':'薪资福利是什么样的','answer':'薪酬采用固定底薪＋绩效工资＋提成的结构，整体方案合理、有竞争力，具体安排可以进一步详聊。','enabled':True,'variants':[]}]}
 person={'outbox':[{'status':'sent','text':'固定底薪＋绩效工资＋提成，具体可以详聊。'}]}
 r=answer_question(c,question,person=person)
 assert r['action']=='answer' and r['source']=='compensation_policy'
 assert '核实' not in r['text'] and '竞争力' in r['text'] and '固定底薪＋' not in r['text']
 check_compensation_disclosure(r['text'])

def test_compound_salary_and_other_question_not_swallowed_by_boundary():
 from cloakup.answering import answer_question
 r=answer_question({'faq':[]},'底薪多少，有社保吗？')
 assert r['action']=='defer'


APPROVED={'question':'薪资福利是什么样的','answer':'薪酬采用固定底薪＋绩效工资＋提成的结构，整体方案合理、有竞争力，具体安排可以进一步详聊。','enabled':True,'variants':[]}

@pytest.mark.parametrize('question',['薪酬结构如何','底薪、提成是如何的？','薪资待遇怎么样','工资怎么算'])
def test_compensation_structure_question_is_answered_not_deferred(question):
 # 11-15 候选人 17、22 问薪酬结构，被当成缺事实只回了“需顾问核实”。
 from cloakup.answering import answer_question
 r=answer_question({'faq':[APPROVED]},question)
 assert r['action']=='answer' and r['source']=='compensation_policy'
 assert '固定底薪＋绩效工资＋提成' in r['text'] and '核实' not in r['text']
 check_compensation_disclosure(r['text'])

def test_structure_question_without_approved_structure_still_defers():
 from cloakup.answering import answer_question
 assert answer_question({'faq':[]},'薪酬结构如何')['action']=='defer'
 assert answer_question({'faq':[APPROVED]},'薪资待遇上班时间方便讲一下吗？')['action']=='defer'
