import asyncio
from types import SimpleNamespace as NS
import pytest
from cloakup.workbench import Workbench
from cloakup.account_execution import AccountExecution
from cloakup.storage import Database
from cloakup.autopilot import run_account_executor


def test_release_closes_retained_instance_but_preserves_profile_and_pause(tmp_path):
 m=Workbench(tmp_path)
 profile=tmp_path/'profiles'/'boss';profile.mkdir(parents=True);(profile/'test-marker').write_text('retained')
 m.account_execution.set_task('task',['case'],'paused');closed=[]
 class Session:
  context=object();page=NS(is_closed=lambda:False)
  async def close(self):closed.append(True);self.context=None
 m.boss_session=Session()
 try:
  assert m.call(m.release_browser_seat())['status']=='released'
  assert closed and (profile/'test-marker').read_text()=='retained'
  assert m.account_execution.members()[0]['status']=='paused'
 finally:m.close()


def test_release_rejects_active_members(tmp_path):
 m=Workbench(tmp_path);m.account_execution.set_task('task',['case'],'running')
 try:
  with pytest.raises(ValueError,match='先暂停'):m.call(m.release_browser_seat())
 finally:m.close()


async def test_standby_yields_when_another_job_is_queued(tmp_path):
 a=AccountExecution(Database(tmp_path));a.set_task('task',['case'],'paused')
 async def checkpoint(job):pass
 manager=NS(account_execution=a,jobs={'next':{'id':'next','status':'queued'}},checkpoint=checkpoint,event=lambda *args:None)
 job={'id':'owner','params':{}}
 result=await run_account_executor(manager,job,object())
 assert result['status']=='succeeded' and job['release_requested']
 assert a.members()[0]['status']=='paused'
