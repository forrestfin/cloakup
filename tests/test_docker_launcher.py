"""Deploy config precedence, without contacting Docker or using real credentials."""
import json
import runpy
import subprocess
import sys
from pathlib import Path
import pytest


def test_saved_feishu_file_survives_empty_container(tmp_path,monkeypatch):
    original=Path(__file__).resolve().parents[1]/'scripts'/'docker_up.py'
    script=tmp_path/'scripts'/'docker_up.py';script.parent.mkdir();script.write_text(original.read_text())
    folder=tmp_path/'data'/'docker';folder.mkdir(parents=True)
    (folder/'postgres-password').write_text('synthetic-test-password')
    (folder/'lark.env').write_text('LARK_APP_ID=cli_saved\nLARK_APP_SECRET=saved-secret\n')
    for key in ('LARK_APP_ID','LARK_APP_SECRET'):monkeypatch.delenv(key,raising=False)
    captured=[]
    def output(args,**kwargs):
        if args[:3]==['docker','compose','ps']:return 'test-container'
        return json.dumps([{'Config':{'Env':['LARK_APP_ID=','LARK_APP_SECRET=']}}])
    monkeypatch.setattr(subprocess,'check_output',output)
    monkeypatch.setattr(subprocess,'call',lambda args,**kwargs:captured.append(kwargs['env']) or 0)
    monkeypatch.setattr(sys,'argv',[str(script),'--reuse-runtime'])
    with pytest.raises(SystemExit) as result:runpy.run_path(str(script),run_name='__main__')
    assert result.value.code==0
    assert captured[0]['LARK_APP_ID']=='cli_saved' and captured[0]['LARK_APP_SECRET']=='saved-secret'


@pytest.mark.parametrize('caller,stored,expected',[
 ({'LARK_APP_ID':'cli_new','LARK_APP_SECRET':'new-secret'},{'LARK_APP_ID':'','LARK_APP_SECRET':''},{'LARK_APP_ID':'cli_new','LARK_APP_SECRET':'new-secret'}),
 ({},{'LARK_APP_ID':'cli_existing','LARK_APP_SECRET':'existing-secret'},{'LARK_APP_ID':'cli_existing','LARK_APP_SECRET':'existing-secret'}),
 ({'LARK_APP_SECRET':''},{'LARK_APP_ID':'cli_existing','LARK_APP_SECRET':'existing-secret'},{'LARK_APP_ID':'cli_existing','LARK_APP_SECRET':''}),
])
def test_reuse_runtime_preserves_explicit_feishu_credentials(tmp_path,monkeypatch,caller,stored,expected):
 original=Path(__file__).resolve().parents[1]/'scripts'/'docker_up.py'
 script=tmp_path/'scripts'/'docker_up.py';script.parent.mkdir();script.write_text(original.read_text())
 password=tmp_path/'data'/'docker'/'postgres-password';password.parent.mkdir(parents=True);password.write_text('synthetic-test-password')
 for key in ('LARK_APP_ID','LARK_APP_SECRET'):monkeypatch.delenv(key,raising=False)
 for key,value in caller.items():monkeypatch.setenv(key,value)
 captured=[]
 def output(args,**kwargs):
  if args[:3]==['docker','compose','ps']:return 'test-container'
  assert args[:2]==['docker','inspect']
  return json.dumps([{'Config':{'Env':[k+'='+v for k,v in stored.items()]}}])
 monkeypatch.setattr(subprocess,'check_output',output)
 monkeypatch.setattr(subprocess,'call',lambda args,**kwargs:captured.append(kwargs['env']) or 0)
 monkeypatch.setattr(sys,'argv',[str(script),'--reuse-runtime'])
 with pytest.raises(SystemExit) as result:runpy.run_path(str(script),run_name='__main__')
 assert result.value.code==0
 assert {k:captured[0].get(k) for k in expected}==expected


@pytest.mark.parametrize('listed,expected',[('stopped-container\n','stopped-container'),('',None)])
def test_reuse_runtime_reads_a_stopped_container(tmp_path,monkeypatch,listed,expected):
    """服务停掉（docker compose stop）后重建，仍复用原容器里的凭据；容器被删除时明确报错。"""
    original=Path(__file__).resolve().parents[1]/'scripts'/'docker_up.py'
    script=tmp_path/'scripts'/'docker_up.py';script.parent.mkdir();script.write_text(original.read_text())
    password=tmp_path/'data'/'docker'/'postgres-password';password.parent.mkdir(parents=True);password.write_text('synthetic-test-password')
    for key in ('ANTHROPIC_API_KEY','CLOAKBROWSER_LICENSE_KEY'):monkeypatch.delenv(key,raising=False)
    seen=[];captured=[]
    def output(args,**kwargs):
        seen.append(args)
        if args[:3]==['docker','compose','ps']:
            assert '-a' in args
            return listed
        assert args==['docker','inspect',expected]
        return json.dumps([{'Config':{'Env':['ANTHROPIC_API_KEY=kept-model-key','CLOAKBROWSER_LICENSE_KEY=kept-license']}}])
    monkeypatch.setattr(subprocess,'check_output',output)
    monkeypatch.setattr(subprocess,'call',lambda args,**kwargs:captured.append(kwargs['env']) or 0)
    monkeypatch.setattr(sys,'argv',[str(script),'--reuse-runtime'])
    with pytest.raises(SystemExit) as result:runpy.run_path(str(script),run_name='__main__')
    if expected:
        assert result.value.code==0
        assert captured[0]['ANTHROPIC_API_KEY']=='kept-model-key' and captured[0]['CLOAKBROWSER_LICENSE_KEY']=='kept-license'
    else:
        assert 'docker compose down' in str(result.value.code) and not captured
