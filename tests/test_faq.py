import pytest
from cloakup.faq import match_faq, checked_faq, needs_confirmation
from cloakup.sourcing import SourcingError
from cloakup.attention import AttentionStore
from cloakup.storage import Database


def faq():
    return {'question':'需要坐班吗？','answer':'该岗位支持远程协作。','variants':['是否需要坐班？'],'enabled':True}


def test_full_question_only_and_ambiguous_match_fails_closed():
    c={'faq':[faq()]}
    assert match_faq(c,' 是否需要坐班? ')['answer']=='该岗位支持远程协作。'
    assert match_faq(c,'需要坐班吗？薪资多少？') is None
    assert match_faq({'faq':[faq(),faq()]},'需要坐班吗？') is None
    c['faq'][0]['enabled']=False
    assert match_faq(c,'需要坐班吗？') is None


def test_faq_rejects_unconfirmed_promises_contact_and_duplicate_questions():
    checked_faq([faq()])
    for text in ('保证录用','电话13812345678',''):
        f=faq();f['answer']=text
        with pytest.raises(SourcingError):checked_faq([f])
    with pytest.raises(SourcingError):checked_faq([faq(),faq()])


def test_question_todos_include_local_source_and_related_count(tmp_path):
    s=AttentionStore(Database(tmp_path))
    c={'id':'job','job':{'label':'测试岗位'},'candidates':{str(i):{'id':str(i),'inbox':[{'external_id':'m','text':q}]} for i,q in enumerate(['社保怎么缴纳？','合同怎么签？'])}}
    todos=s.reconcile([], [c])
    assert len(todos)==2 and all(t['related_count']==2 and t['question'] for t in todos)
    c['faq']=[{'question':'社保怎么缴纳？','answer':'目前需由顾问确认。','variants':[],'enabled':True}]
    todos=s.reconcile([], [c])
    assert sum(t['status']!='resolved' for t in todos)==1
    assert next(t for t in todos if t['status']!='resolved')['related_count']==1
    assert needs_confirmation('能否介绍一下工作时间')
